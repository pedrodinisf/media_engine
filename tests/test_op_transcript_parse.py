"""Tests for ops/transcript/parse.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_engine.artifacts import Kind, Transcript
from media_engine.ops import OperationContext
from media_engine.ops.transcript.parse import (
    OP_NAME,
    ParseParams,
    TranscriptParse,
)
from media_engine.runtime.engine import Engine

# ─────────────────────────────────────────────────────────────────
# Op contract
# ─────────────────────────────────────────────────────────────────


def test_op_class_attributes() -> None:
    assert TranscriptParse.name == "transcript.parse"
    assert TranscriptParse.input_kinds == ()
    assert TranscriptParse.output_kinds == (Kind.Transcript,)
    assert TranscriptParse.default_backend is None


def test_cost_estimate_scales_with_size(tmp_path: Path) -> None:
    f = tmp_path / "x.srt"
    f.write_text("ignored")
    est = TranscriptParse().cost_estimate(
        [], ParseParams(source_path=f, format="srt")
    )
    assert est.local_seconds > 0


# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────

_SRT = """1
00:00:00,000 --> 00:00:01,500
Hello world.

2
00:00:01,500 --> 00:00:03,250
This is the second cue.
"""

_VTT = """WEBVTT

00:00:00.000 --> 00:00:01.500
Hello world.

00:00:01.500 --> 00:00:03.250
This is the second cue.
"""

_SPEAKERED_TXT = """[SPEAKER_00]
Good morning everyone.

[SPEAKER_01]
Glad to be here.
And looking forward to it.

[SPEAKER_00]
Let's begin.
"""


@pytest.fixture
def srt_file(tmp_path: Path) -> Path:
    p = tmp_path / "t.srt"
    p.write_text(_SRT)
    return p


@pytest.fixture
def vtt_file(tmp_path: Path) -> Path:
    p = tmp_path / "t.vtt"
    p.write_text(_VTT)
    return p


@pytest.fixture
def speakered_file(tmp_path: Path) -> Path:
    p = tmp_path / "t.txt"
    p.write_text(_SPEAKERED_TXT)
    return p


# ─────────────────────────────────────────────────────────────────
# Direct op.run + parser correctness
# ─────────────────────────────────────────────────────────────────


async def test_parse_srt(
    op_ctx: OperationContext, srt_file: Path
) -> None:
    [t] = await TranscriptParse().run(
        [], ParseParams(source_path=srt_file, format="srt"), op_ctx
    )
    assert isinstance(t, Transcript)
    assert len(t.segments) == 2
    assert t.segments[0]["start"] == 0.0
    assert t.segments[0]["end"] == 1.5
    assert t.segments[0]["text"] == "Hello world."
    assert t.segments[1]["start"] == 1.5
    assert t.segments[1]["text"] == "This is the second cue."
    assert all(s["speaker_id"] is None for s in t.segments)
    assert t.metadata["source_format"] == "srt"


async def test_parse_vtt(
    op_ctx: OperationContext, vtt_file: Path
) -> None:
    [t] = await TranscriptParse().run(
        [], ParseParams(source_path=vtt_file, format="vtt"), op_ctx
    )
    assert len(t.segments) == 2
    assert t.segments[0]["start"] == 0.0
    assert t.segments[0]["end"] == 1.5
    assert t.segments[1]["text"] == "This is the second cue."


async def test_parse_speakered_txt_preserves_speakers(
    op_ctx: OperationContext, speakered_file: Path
) -> None:
    [t] = await TranscriptParse().run(
        [],
        ParseParams(source_path=speakered_file, format="speakered_txt"),
        op_ctx,
    )
    ids = [s["speaker_id"] for s in t.segments]
    assert ids == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert (
        t.segments[1]["text"]
        == "Glad to be here. And looking forward to it."
    )
    assert all(s["start"] is None for s in t.segments)


async def test_srt_round_trip_text_preserved(
    op_ctx: OperationContext, srt_file: Path
) -> None:
    """Parse → reconstruct SRT → re-parse → same segments."""
    [t1] = await TranscriptParse().run(
        [], ParseParams(source_path=srt_file, format="srt"), op_ctx
    )

    def _fmt(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int(round((sec - int(sec)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    rebuilt = "\n".join(
        f"{i + 1}\n{_fmt(seg['start'])} --> {_fmt(seg['end'])}\n{seg['text']}\n"
        for i, seg in enumerate(t1.segments)
    )
    rt = srt_file.with_name("rt.srt")
    rt.write_text(rebuilt)
    [t2] = await TranscriptParse().run(
        [], ParseParams(source_path=rt, format="srt"), op_ctx
    )
    assert [s["text"] for s in t1.segments] == [s["text"] for s in t2.segments]
    assert [s["start"] for s in t1.segments] == [s["start"] for s in t2.segments]


# ─────────────────────────────────────────────────────────────────
# Engine.run dispatch / cache / param-change
# ─────────────────────────────────────────────────────────────────


async def test_engine_run_caches_on_rerun(
    engine: Engine, srt_file: Path, mocker
) -> None:
    [t1] = await engine.run(
        OP_NAME, source_path=srt_file, format="srt"
    )
    spy = mocker.spy(TranscriptParse, "run")
    [t2] = await engine.run(
        OP_NAME, source_path=srt_file, format="srt"
    )
    assert spy.call_count == 0  # served from cache
    assert t1.id == t2.id


async def test_format_change_yields_new_id(
    engine: Engine, tmp_path: Path
) -> None:
    """Same bytes parsed under a different format → different Transcript id."""
    # An SRT cue happens to be a valid (if odd-looking) speakered_txt input
    # — the *parse output* differs, and the derived id must reflect that.
    f = tmp_path / "ambiguous.txt"
    f.write_text(_SPEAKERED_TXT)
    [a] = await engine.run(OP_NAME, source_path=f, format="speakered_txt")
    # A second file with identical content parsed as srt → another id.
    g = tmp_path / "ambiguous.srt"
    g.write_text(_SPEAKERED_TXT)
    [b] = await engine.run(OP_NAME, source_path=g, format="srt")
    assert a.id != b.id


# ─────────────────────────────────────────────────────────────────
# Error paths
# ─────────────────────────────────────────────────────────────────


async def test_missing_file_raises(op_ctx: OperationContext, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await TranscriptParse().run(
            [],
            ParseParams(source_path=tmp_path / "nope.srt", format="srt"),
            op_ctx,
        )


def test_unknown_format_rejected_by_pydantic(tmp_path: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ParseParams(source_path=tmp_path / "f.txt", format="bogus")  # type: ignore[arg-type]


async def test_rejects_inputs(
    op_ctx: OperationContext, sample_mp4: Path
) -> None:
    from media_engine.ops.acquire.upload import (
        AcquireUpload,
        AcquireUploadParams,
    )

    [v] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), op_ctx
    )
    with pytest.raises(ValueError, match="takes no inputs"):
        await TranscriptParse().run(
            [v],
            ParseParams(source_path=sample_mp4, format="srt"),
            op_ctx,
        )
