"""Tests for media_engine.runtime.audio_slice.

The helper is shared between the mlx-whisper and pyannote backends —
they both call ``maybe_slice_audio`` immediately before invoking their
underlying ML library. We verify:

* No params set → passes the original path through (no subprocess).
* Both params set → emits a temp file in the workdir, validates window.
* Validator on the calling Params model rejects ``end_s <= start_s``.
* Missing ffmpeg surfaces as RuntimeError (not a silent fail).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from media_engine.ops.audio.diarize import DiarizeParams
from media_engine.ops.audio.transcribe import TranscribeParams
from media_engine.ops.audio.transcribe_diarized import TranscribeDiarizedParams
from media_engine.runtime.audio_slice import maybe_slice_audio


def _make_ctx(workdir: Path, ffmpeg: str = "ffmpeg") -> SimpleNamespace:
    """Minimal duck-typed OperationContext for the helper.

    The helper only reads ``ctx.config.ffmpeg_path`` and ``ctx.workdir``,
    so a SimpleNamespace beats spinning up a real Engine + cache for
    every test.
    """
    return SimpleNamespace(
        workdir=workdir,
        config=SimpleNamespace(ffmpeg_path=ffmpeg),
    )


@pytest.fixture
def fake_audio(tmp_path: Path) -> Path:
    """Synthesize a tiny silent WAV (1 second, 44.1 kHz mono) via ffmpeg.

    Skipped when ffmpeg isn't on PATH — the slice helper depends on it
    too, so there's no point exercising the path-through case alone.
    """
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    wav = tmp_path / "src.wav"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100",
            "-t", "5",
            str(wav),
        ],
        check=True, capture_output=True,
    )
    return wav


def test_no_range_returns_original_path(tmp_path: Path) -> None:
    """Both params None ⇒ no ffmpeg, original path passes through."""
    ctx = _make_ctx(tmp_path)
    src = tmp_path / "input.wav"
    src.write_bytes(b"")  # the helper never reads the file in this branch
    out = maybe_slice_audio(src, start_s=None, end_s=None, ctx=ctx)
    assert out == str(src)


def test_slice_creates_temp_file(tmp_path: Path, fake_audio: Path) -> None:
    """start_s + end_s set ⇒ helper writes a sliced file in workdir."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    ctx = _make_ctx(workdir)
    out = maybe_slice_audio(
        fake_audio, start_s=1.0, end_s=2.5, ctx=ctx
    )
    out_path = Path(out)
    assert out_path != fake_audio
    assert out_path.parent == workdir
    assert out_path.exists()
    assert out_path.stat().st_size > 0


def test_missing_ffmpeg_raises_runtime_error(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, ffmpeg="ffmpeg-does-not-exist")
    with pytest.raises(RuntimeError, match="ffmpeg binary not found"):
        maybe_slice_audio(
            tmp_path / "anything.wav",
            start_s=0.0, end_s=1.0, ctx=ctx,
        )


# ─────────────────────────────────────────────────────────────────
# Range validators on the three Params models
# ─────────────────────────────────────────────────────────────────


def test_transcribe_params_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="end_s must be > start_s"):
        TranscribeParams(start_s=5.0, end_s=2.0)


def test_diarize_params_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="end_s must be > start_s"):
        DiarizeParams(start_s=10.0, end_s=10.0)  # equal → invalid


def test_transcribe_diarized_params_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="end_s must be > start_s"):
        TranscribeDiarizedParams(start_s=30.0, end_s=15.0)


def test_open_ended_ranges_are_valid() -> None:
    """start without end ⇒ from X to EOF; end without start ⇒ from 0 to X."""
    TranscribeParams(start_s=5.0)             # no end_s
    TranscribeParams(end_s=180.0)             # no start_s
    DiarizeParams(start_s=0.0, end_s=60.0)    # both, in order
    TranscribeDiarizedParams()                # both None — defaults


def test_negative_start_or_end_rejected() -> None:
    """Field(ge=0.0) blocks negatives at the field level."""
    with pytest.raises(ValueError):
        TranscribeParams(start_s=-1.0)
    with pytest.raises(ValueError):
        DiarizeParams(end_s=-0.5)
