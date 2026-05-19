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
SAMPLE_DIALOGUE_WAV = FIXTURE_DIR / "sample_dialogue.wav"
SAMPLE_PNG = FIXTURE_DIR / "sample.png"
TINY_HLS_DIR = FIXTURE_DIR / "tiny_hls"

OCR_TEXT = "MEDIA ENGINE"

SPEECH_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "Pack my box with five dozen liquor jugs."
)
DIALOGUE_VOICE_A = "Alex"
DIALOGUE_VOICE_B = "Samantha"
DIALOGUE_LINE_A = (
    "Good morning. The quarterly results exceeded our internal forecasts."
)
DIALOGUE_LINE_B = (
    "That's terrific news. Does this change our hiring plan for next quarter?"
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


def build_sample_dialogue_wav(force: bool = False) -> None:
    """Two-speaker synthesized dialogue: ``say`` × 2 voices + ffmpeg concat.

    16 kHz mono pcm_s16le wav with two distinct synthetic voices speaking in
    turn — enough signal for pyannote to detect 2 speakers in
    ``audio.diarize`` tests.
    """
    if SAMPLE_DIALOGUE_WAV.exists() and not force:
        return
    if shutil.which("say") is None:
        sys.stderr.write("skipping sample_dialogue.wav: macOS `say` not found\n")
        return
    _require_ffmpeg()
    a_aiff = FIXTURE_DIR / "_diag_a.aiff"
    b_aiff = FIXTURE_DIR / "_diag_b.aiff"
    a_wav = FIXTURE_DIR / "_diag_a.wav"
    b_wav = FIXTURE_DIR / "_diag_b.wav"
    list_file = FIXTURE_DIR / "_diag_list.txt"
    try:
        subprocess.check_call(
            ["say", "-v", DIALOGUE_VOICE_A, "-o", str(a_aiff), DIALOGUE_LINE_A],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.check_call(
            ["say", "-v", DIALOGUE_VOICE_B, "-o", str(b_aiff), DIALOGUE_LINE_B],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for src, dst in ((a_aiff, a_wav), (b_aiff, b_wav)):
            subprocess.check_call(
                [
                    "ffmpeg", "-y", "-i", str(src),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                    str(dst),
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        # ffmpeg concat demuxer expects a manifest with `file '...'` lines.
        list_file.write_text(f"file '{a_wav.name}'\nfile '{b_wav.name}'\n")
        subprocess.check_call(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c:a", "pcm_s16le",
                str(SAMPLE_DIALOGUE_WAV),
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    finally:
        for f in (a_aiff, b_aiff, a_wav, b_wav, list_file):
            f.unlink(missing_ok=True)


def build_sample_png(force: bool = False) -> None:
    """A small PNG with the literal text ``MEDIA ENGINE`` rendered on it.

    Used by ``image.{describe,ocr,classify}`` tests. ``drawtext`` needs an
    ffmpeg with libfreetype; if that's unavailable we fall back to a plain
    ``testsrc`` pattern (still a valid Image — OCR real-smoke tests skip
    cleanly when the text can't be rendered or rapidocr isn't installed).
    """
    if SAMPLE_PNG.exists() and not force:
        return
    _require_ffmpeg()
    drawtext = (
        "color=c=white:s=480x160:d=1,"
        f"drawtext=text='{OCR_TEXT}':fontcolor=black:fontsize=64:"
        "x=(w-text_w)/2:y=(h-text_h)/2"
    )
    try:
        subprocess.check_call(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", drawtext,
                "-frames:v", "1",
                str(SAMPLE_PNG),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        # No libfreetype → still emit a valid PNG so non-OCR tests run.
        subprocess.check_call(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc=size=480x160:rate=1",
                "-frames:v", "1",
                str(SAMPLE_PNG),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def build_tiny_hls(force: bool = False) -> None:
    """A 2 s synthetic HLS stream: ``index.m3u8`` + tiny ``.ts`` segments.

    Served by a stdlib ``http.server`` in the ``playwright-hls`` /
    ``acquire.livestream`` tests so they never touch the network. Total
    payload stays well under 200 KB.
    """
    playlist = TINY_HLS_DIR / "index.m3u8"
    if playlist.exists() and not force:
        return
    _require_ffmpeg()
    TINY_HLS_DIR.mkdir(parents=True, exist_ok=True)
    for old in TINY_HLS_DIR.glob("*.ts"):
        old.unlink()
    # Force a keyframe every 1 s (``-g 10`` at 10 fps + ``-keyint_min 10`` +
    # ``-sc_threshold 0``) so stream-copy segmenters can cut on whole-second
    # boundaries — the ``acquire.livestream`` segment-muxer test depends on it.
    subprocess.check_call(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-g", "10", "-keyint_min", "10", "-sc_threshold", "0",
            "-c:a", "aac", "-shortest",
            "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
            "-hls_segment_filename", str(TINY_HLS_DIR / "seg_%03d.ts"),
            str(playlist),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true", help="rebuild even if present")
    args = parser.parse_args()

    build_sample_mp4(force=args.rebuild)
    build_sample_m4a(force=args.rebuild)
    build_corrupt_mp4(force=args.rebuild)
    build_sample_speech_wav(force=args.rebuild)
    build_sample_dialogue_wav(force=args.rebuild)
    build_sample_png(force=args.rebuild)
    build_tiny_hls(force=args.rebuild)

    print(f"Fixtures ready in {FIXTURE_DIR}")
    for f in (SAMPLE_MP4, SAMPLE_M4A, CORRUPT_MP4, SAMPLE_SPEECH_WAV,
              SAMPLE_DIALOGUE_WAV, SAMPLE_PNG):
        size = f.stat().st_size if f.exists() else "(missing)"
        print(f"  {f.name:<22} = {size}")


if __name__ == "__main__":
    main()
