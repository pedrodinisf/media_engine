"""Synthesize tiny media fixtures for tests.

Idempotent: only rebuilds files that don't exist (use ``--rebuild`` to force).
All fixtures stay <500 KB so they're fine to commit. Built with ffmpeg lavfi
so no external assets are needed.

Usage:
    uv run python tests/fixtures/build_fixtures.py
    uv run python tests/fixtures/build_fixtures.py --rebuild
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent

SAMPLE_MP4 = FIXTURE_DIR / "sample.mp4"
SAMPLE_M4A = FIXTURE_DIR / "sample.m4a"
CORRUPT_MP4 = FIXTURE_DIR / "corrupt.mp4"
SAMPLE_SPEECH_WAV = FIXTURE_DIR / "sample_speech.wav"

SPEECH_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "Pack my box with five dozen liquor jugs."
)


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found in PATH; install via `brew install ffmpeg`.")


def build_sample_mp4(force: bool = False) -> None:
    if SAMPLE_MP4.exists() and not force:
        return
    _require_ffmpeg()
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-shortest",
            str(SAMPLE_MP4),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def build_sample_m4a(force: bool = False) -> None:
    if SAMPLE_M4A.exists() and not force:
        return
    _require_ffmpeg()
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            "-c:a", "aac",
            "-b:a", "64k",
            str(SAMPLE_M4A),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def build_corrupt_mp4(force: bool = False) -> None:
    if CORRUPT_MP4.exists() and not force:
        return
    CORRUPT_MP4.write_text("This file looks like an mp4 but it isn't.\n")


def build_sample_speech_wav(force: bool = False) -> None:
    """16 kHz mono pcm_s16le speech via macOS `say` + ffmpeg.

    Skipped when `say` is unavailable (e.g. CI on Linux); the corresponding
    test fixtures will skip cleanly with a clear pytest.skip message.
    """
    if SAMPLE_SPEECH_WAV.exists() and not force:
        return
    if shutil.which("say") is None:
        sys.stderr.write(
            "skipping sample_speech.wav: macOS `say` binary not found\n"
        )
        return
    _require_ffmpeg()
    aiff = FIXTURE_DIR / "_speech.aiff"
    try:
        subprocess.check_call(
            ["say", "-o", str(aiff), SPEECH_TEXT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.check_call(
            [
                "ffmpeg", "-y",
                "-i", str(aiff),
                "-ar", "16000", "-ac", "1",
                "-c:a", "pcm_s16le",
                str(SAMPLE_SPEECH_WAV),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        aiff.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true", help="rebuild even if present")
    args = parser.parse_args()

    build_sample_mp4(force=args.rebuild)
    build_sample_m4a(force=args.rebuild)
    build_corrupt_mp4(force=args.rebuild)
    build_sample_speech_wav(force=args.rebuild)

    print(f"Fixtures ready in {FIXTURE_DIR}")
    for f in (SAMPLE_MP4, SAMPLE_M4A, CORRUPT_MP4, SAMPLE_SPEECH_WAV):
        size = f.stat().st_size if f.exists() else "(missing)"
        print(f"  {f.name:<22} = {size}")


if __name__ == "__main__":
    main()
