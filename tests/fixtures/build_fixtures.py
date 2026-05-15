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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true", help="rebuild even if present")
    args = parser.parse_args()

    build_sample_mp4(force=args.rebuild)
    build_sample_m4a(force=args.rebuild)
    build_corrupt_mp4(force=args.rebuild)

    print(
        f"Fixtures ready in {FIXTURE_DIR}\n"
        f"  sample.mp4  = {SAMPLE_MP4.stat().st_size:>7} bytes\n"
        f"  sample.m4a  = {SAMPLE_M4A.stat().st_size:>7} bytes\n"
        f"  corrupt.mp4 = {CORRUPT_MP4.stat().st_size:>7} bytes"
    )


if __name__ == "__main__":
    main()
