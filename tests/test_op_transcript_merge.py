"""Tests for ops/transcript/merge.py."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from media_engine.artifacts import Kind, Transcript
from media_engine.ops import OperationContext
from media_engine.ops.transcript.merge import (
    OP_NAME,
    TranscriptMerge,
    merge_segments,
)
from media_engine.runtime.engine import Engine


def test_op_class_attributes() -> None:
    assert TranscriptMerge.name == "transcript.merge"
    assert TranscriptMerge.input_kinds == (Kind.Transcript,)
    assert TranscriptMerge.output_kinds == (Kind.Transcript,)
    assert TranscriptMerge.default_backend is None


# ─────────────────────────────────────────────────────────────────
# Pure merge_segments — boundary semantics
# ─────────────────────────────────────────────────────────────────


def _seg(start: float | None, end: float | None, sp: str | None, text: str) -> dict[str, Any]:
    return {"start": start, "end": end, "speaker_id": sp, "text": text}


def test_collapses_same_speaker_close_gap() -> None:
    out = merge_segments(
        [
            _seg(0.0, 1.0, "A", "Hello"),
            _seg(1.5, 2.5, "A", "world"),
            _seg(2.7, 3.5, "A", "again"),
        ],
        gap_threshold_sec=2.0,
        max_chars=2000,
    )
    assert len(out) == 1
    assert out[0]["speaker_id"] == "A"
    assert out[0]["text"] == "Hello world again"
    assert out[0]["start"] == 0.0
    assert out[0]["end"] == 3.5


def test_speaker_change_breaks_boundary() -> None:
    out = merge_segments(
        [
            _seg(0.0, 1.0, "A", "a1"),
            _seg(1.1, 2.0, "B", "b1"),
            _seg(2.1, 3.0, "B", "b2"),
            _seg(3.1, 4.0, "A", "a2"),
        ],
        gap_threshold_sec=2.0,
        max_chars=2000,
    )
    assert [s["speaker_id"] for s in out] == ["A", "B", "A"]
    assert out[1]["text"] == "b1 b2"


def test_gap_over_threshold_breaks_boundary() -> None:
    out = merge_segments(
        [
            _seg(0.0, 1.0, "A", "first"),
            _seg(10.0, 11.0, "A", "second"),
        ],
        gap_threshold_sec=2.0,
        max_chars=2000,
    )
    assert len(out) == 2


def test_max_chars_breaks_boundary() -> None:
    out = merge_segments(
        [
            _seg(0.0, 1.0, "A", "x" * 60),
            _seg(1.5, 2.0, "A", "y" * 60),
            _seg(2.5, 3.0, "A", "z" * 60),
        ],
        gap_threshold_sec=2.0,
        max_chars=100,
    )
    # 60+60>100 → boundary; 60+60>100 → boundary again
    assert len(out) == 3


def test_no_timestamps_collapses_same_speaker_runs() -> None:
    out = merge_segments(
        [
            _seg(None, None, "A", "one"),
            _seg(None, None, "A", "two"),
            _seg(None, None, "B", "three"),
        ],
        gap_threshold_sec=2.0,
        max_chars=2000,
    )
    assert [s["speaker_id"] for s in out] == ["A", "B"]
    assert out[0]["text"] == "one two"


def test_empty_input_returns_empty() -> None:
    assert merge_segments([], gap_threshold_sec=2.0, max_chars=2000) == []


# ─────────────────────────────────────────────────────────────────
# Engine.run dispatch / cache / param-change
# ─────────────────────────────────────────────────────────────────


def _build_transcript(engine: Engine) -> Transcript:
    """Construct a Transcript artifact directly and register it."""
    import json

    from media_engine.artifacts import compute_derived_artifact_id

    segs = [
        _seg(0.0, 1.0, "A", "Hi."),
        _seg(1.3, 2.0, "A", "How are you?"),
        _seg(5.0, 6.0, "B", "Fine, thanks."),
    ]
    payload = {
        "text": "Hi. How are you? Fine, thanks.",
        "segments": segs,
        "language": "en",
        "model": "parser:srt",
    }
    derived_id = compute_derived_artifact_id(
        kind=Kind.Transcript,
        op_name="test.synth",
        op_version="1",
        backend_name=None,
        backend_version=None,
        params={"key": "synth"},
        input_ids=[],
    )
    tmp = engine.storage.ensure_workdir("synth") / "t.json"
    tmp.write_text(json.dumps(payload))
    dest = engine.storage.store_file(tmp, derived_id, ".json")
    t = Transcript(
        id=derived_id, path=dest, metadata=payload,
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(t)
    return t


async def test_engine_run_merge_uses_gap_and_speaker(engine: Engine) -> None:
    t = _build_transcript(engine)
    [m] = await engine.run(OP_NAME, inputs=[t.id], gap_threshold_sec=2.0)
    assert m.derived_from == (t.id,)
    # Same speaker A within gap → collapses; B separated by 3s → its own seg.
    assert [s["speaker_id"] for s in m.segments] == ["A", "B"]


async def test_engine_run_merge_cache_hit(engine: Engine, mocker) -> None:
    t = _build_transcript(engine)
    [m1] = await engine.run(OP_NAME, inputs=[t.id], gap_threshold_sec=2.0)
    spy = mocker.spy(TranscriptMerge, "run")
    [m2] = await engine.run(OP_NAME, inputs=[t.id], gap_threshold_sec=2.0)
    assert spy.call_count == 0
    assert m1.id == m2.id


async def test_engine_run_merge_param_change(engine: Engine) -> None:
    t = _build_transcript(engine)
    [m1] = await engine.run(OP_NAME, inputs=[t.id], gap_threshold_sec=2.0)
    [m2] = await engine.run(OP_NAME, inputs=[t.id], gap_threshold_sec=0.1)
    assert m1.id != m2.id  # tighter threshold splits the A run apart


async def test_engine_run_merge_rejects_wrong_kind(
    engine: Engine, sample_mp4: Path
) -> None:
    from media_engine.ops.acquire.upload import (
        AcquireUpload,
        AcquireUploadParams,
    )

    workdir = engine.storage.ensure_workdir("t")
    ctx = OperationContext(
        workdir=workdir, config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace,
    )
    [v] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), ctx
    )
    engine.cache.upsert_artifact(v)
    with pytest.raises(ValueError, match="kind mismatch"):
        await engine.run(OP_NAME, inputs=[v.id])
