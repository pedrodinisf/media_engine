"""Tests for ops/speakers/identify.py + the pure-function speaker_db helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from media_engine.artifacts import Kind, Transcript, compute_derived_artifact_id
from media_engine.ops.speakers._speaker_db import (
    SpeakerEntry,
    identify_speakers,
    load_speaker_db,
    text_per_cluster,
)
from media_engine.ops.speakers.identify import (
    OP_NAME,
    IdentifyParams,
    SpeakersIdentify,
)
from media_engine.runtime.engine import Engine

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SPEAKERS_CSV = FIXTURE_DIR / "speakers.csv"


# ─────────────────────────────────────────────────────────────────
# Pure-function unit tests: load_speaker_db / text_per_cluster /
# identify_speakers — easier to reason about without engine plumbing.
# ─────────────────────────────────────────────────────────────────


def test_load_speaker_db_reads_canonical_and_aliases() -> None:
    db = load_speaker_db(SPEAKERS_CSV)
    by_name = {e.canonical: e for e in db}
    assert "Klaus Anybody" in by_name
    klaus = by_name["Klaus Anybody"]
    assert klaus.candidates[0] == "Klaus Anybody"
    assert "Mr. Anybody" in klaus.candidates
    assert klaus.extra.get("position") == "Chair"


def test_load_speaker_db_missing_file_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="speaker_db not found"):
        load_speaker_db(tmp_path / "missing.csv")


def test_load_speaker_db_missing_name_column_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("firstname,lastname\nKlaus,Anybody\n")
    with pytest.raises(ValueError, match="missing required column"):
        load_speaker_db(bad)


def test_load_speaker_db_missing_alias_column_falls_back_to_canonical(
    tmp_path: Path,
) -> None:
    no_alias = tmp_path / "no_alias.csv"
    no_alias.write_text("name,position\nKlaus Anybody,Chair\n")
    db = load_speaker_db(no_alias)
    assert len(db) == 1
    assert db[0].candidates == ("Klaus Anybody",)
    assert db[0].extra.get("position") == "Chair"


def test_load_speaker_db_empty_alias_cell_yields_canonical_only(
    tmp_path: Path,
) -> None:
    p = tmp_path / "empty_alias.csv"
    p.write_text("name,aliases\nKlaus,\n")
    db = load_speaker_db(p)
    assert db[0].candidates == ("Klaus",)


def test_text_per_cluster_respects_intro_window() -> None:
    segments = [
        {"speaker_id": "SPEAKER_00", "start": 0.0, "end": 5.0, "text": "Hi I'm Klaus."},
        {"speaker_id": "SPEAKER_00", "start": 5.0, "end": 32.0, "text": "Let's discuss."},
        {"speaker_id": "SPEAKER_00", "start": 60.0, "end": 65.0, "text": "Later on..."},
        {"speaker_id": "SPEAKER_01", "start": 6.0, "end": 8.0, "text": "I'm Jane Example."},
    ]
    out = text_per_cluster(segments, intro_window_seconds=30.0)
    assert "Klaus" in out["SPEAKER_00"]
    # The third SPEAKER_00 segment starts past the 30s cutoff — must be excluded.
    assert "Later" not in out["SPEAKER_00"]
    assert "Jane Example" in out["SPEAKER_01"]


def test_text_per_cluster_skips_unknown_cluster() -> None:
    segments = [
        {"speaker_id": "UNKNOWN", "start": 0.0, "end": 5.0, "text": "Mystery."},
        {"speaker_id": "SPEAKER_00", "start": 0.0, "end": 5.0, "text": "Hello."},
    ]
    out = text_per_cluster(segments)
    assert "UNKNOWN" not in out
    assert "SPEAKER_00" in out


def test_text_per_cluster_falls_back_to_all_text_when_short() -> None:
    segments = [
        {"speaker_id": "SPEAKER_00", "start": 0.0, "end": 2.0, "text": "Brief."},
    ]
    out = text_per_cluster(segments, intro_window_seconds=30.0)
    assert out["SPEAKER_00"] == "Brief."


def _db() -> list[SpeakerEntry]:
    return load_speaker_db(SPEAKERS_CSV)


def test_identify_resolves_canonical_above_threshold() -> None:
    matches = identify_speakers(
        {"SPEAKER_00": "Welcome everyone, I am Klaus Anybody, chair of the council."},
        _db(),
        min_confidence=0.7,
    )
    m = matches["SPEAKER_00"]
    assert m is not None
    assert m.canonical == "Klaus Anybody"
    assert m.score >= 70.0


def test_identify_resolves_alias() -> None:
    matches = identify_speakers(
        {"SPEAKER_01": "Hello, I am Dr. Jane and I'd like to start."},
        _db(),
        min_confidence=0.7,
    )
    m = matches["SPEAKER_01"]
    assert m is not None
    assert m.canonical == "Jane Example"


def test_identify_rejects_below_threshold() -> None:
    matches = identify_speakers(
        {"SPEAKER_02": "Today we'll cover the agenda and some logistics."},
        _db(),
        min_confidence=0.95,
    )
    assert matches["SPEAKER_02"] is None


def test_identify_returns_none_for_empty_text() -> None:
    matches = identify_speakers({"SPEAKER_X": ""}, _db())
    assert matches["SPEAKER_X"] is None


def test_identify_empty_db_returns_all_none() -> None:
    matches = identify_speakers(
        {"SPEAKER_00": "Hi I'm Klaus Anybody."}, db=[], min_confidence=0.7
    )
    assert matches == {"SPEAKER_00": None}


# ─────────────────────────────────────────────────────────────────
# Op-class invariants
# ─────────────────────────────────────────────────────────────────


def test_op_class_attributes() -> None:
    assert SpeakersIdentify.name == "speakers.identify"
    assert SpeakersIdentify.input_kinds == (Kind.Transcript,)
    assert SpeakersIdentify.output_kinds == (Kind.Transcript,)
    assert SpeakersIdentify.default_backend is None
    assert SpeakersIdentify.variadic_inputs is False
    # `backend` field would collide with Engine.run kwarg.
    assert "backend" not in IdentifyParams.model_fields


def test_params_speaker_db_sha_auto_populated() -> None:
    p = IdentifyParams(speaker_db=SPEAKERS_CSV)
    assert len(p.speaker_db_sha) == 16  # 16-char sha prefix


def test_params_speaker_db_sha_marks_missing() -> None:
    p = IdentifyParams(speaker_db=Path("/nonexistent/missing.csv"))
    assert p.speaker_db_sha == "missing"


# ─────────────────────────────────────────────────────────────────
# Engine-driven success / cache / param-change tests
# ─────────────────────────────────────────────────────────────────


def _build_diarized_transcript(
    engine: Engine,
    salt: str = "default",
) -> Transcript:
    """Persist a synthetic Transcript with speaker_id-stamped segments —
    mirrors what audio.transcribe_diarized would output.
    """
    segments: list[dict[str, Any]] = [
        {
            "start": 0.0,
            "end": 8.0,
            "text": "Hi everyone, my name is Klaus Anybody and I am chair.",
            "speaker_id": "SPEAKER_00",
        },
        {
            "start": 8.0,
            "end": 25.0,
            "text": "Today we will discuss several topics in turn.",
            "speaker_id": "SPEAKER_00",
        },
        {
            "start": 25.0,
            "end": 35.0,
            "text": "Hello, I am Dr. Jane, director of research at Example Labs.",
            "speaker_id": "SPEAKER_01",
        },
        {
            "start": 35.0,
            "end": 60.0,
            "text": "And we shall now turn to the agenda items in detail.",
            "speaker_id": "SPEAKER_02",
        },
    ]
    payload = {
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "language": "en",
        "model": "parser:test",
        "num_speakers": 3,
    }
    derived_id = compute_derived_artifact_id(
        kind=Kind.Transcript,
        op_name="test.synth",
        op_version="1",
        backend_name=None,
        backend_version=None,
        params={"salt": salt},
        input_ids=[],
    )
    tmp = engine.storage.ensure_workdir("speakers-test") / "t.json"
    tmp.write_text(json.dumps(payload))
    dest = engine.storage.store_file(tmp, derived_id, ".json")
    t = Transcript(
        id=derived_id,
        path=dest,
        metadata=payload,
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(t)
    return t


async def test_engine_run_resolves_clusters(engine: Engine) -> None:
    t = _build_diarized_transcript(engine)
    [m] = await engine.run(
        OP_NAME,
        inputs=[t.id],
        speaker_db=SPEAKERS_CSV,
        min_confidence=0.7,
    )
    assert m.derived_from == (t.id,)
    names: dict[str, str | None] = m.metadata["speaker_names"]
    assert names["SPEAKER_00"] == "Klaus Anybody"
    assert names["SPEAKER_01"] == "Jane Example"
    # SPEAKER_02's text mentions no DB name — must stay unresolved.
    assert names["SPEAKER_02"] is None
    # Per-segment annotation:
    seg_by_speaker = {s["speaker_id"]: s for s in m.metadata["segments"]}
    assert seg_by_speaker["SPEAKER_00"]["speaker_name"] == "Klaus Anybody"
    assert seg_by_speaker["SPEAKER_02"]["speaker_name"] is None


async def test_engine_run_preserves_cluster_ids(engine: Engine) -> None:
    t = _build_diarized_transcript(engine)
    [m] = await engine.run(
        OP_NAME, inputs=[t.id], speaker_db=SPEAKERS_CSV
    )
    sids = sorted({s["speaker_id"] for s in m.metadata["segments"]})
    assert sids == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]


async def test_engine_run_missing_db_errors_with_path(
    engine: Engine, tmp_path: Path
) -> None:
    t = _build_diarized_transcript(engine)
    missing = tmp_path / "missing.csv"
    with pytest.raises(FileNotFoundError, match="speaker_db not found"):
        await engine.run(OP_NAME, inputs=[t.id], speaker_db=missing)


async def test_engine_run_cache_hit_on_rerun(
    engine: Engine, mocker: Any
) -> None:
    t = _build_diarized_transcript(engine)
    [m1] = await engine.run(
        OP_NAME, inputs=[t.id], speaker_db=SPEAKERS_CSV
    )
    spy = mocker.spy(SpeakersIdentify, "run")
    [m2] = await engine.run(
        OP_NAME, inputs=[t.id], speaker_db=SPEAKERS_CSV
    )
    assert spy.call_count == 0
    assert m1.id == m2.id


async def test_engine_run_cache_miss_on_min_confidence_change(
    engine: Engine,
) -> None:
    t = _build_diarized_transcript(engine)
    [m1] = await engine.run(
        OP_NAME, inputs=[t.id], speaker_db=SPEAKERS_CSV, min_confidence=0.7
    )
    [m2] = await engine.run(
        OP_NAME, inputs=[t.id], speaker_db=SPEAKERS_CSV, min_confidence=0.95
    )
    assert m1.id != m2.id


async def test_engine_run_cache_miss_on_csv_change(
    engine: Engine, tmp_path: Path
) -> None:
    """Editing the speaker DB must invalidate the cache via speaker_db_sha."""
    t = _build_diarized_transcript(engine)
    db1 = tmp_path / "db1.csv"
    db1.write_text("name,aliases\nKlaus Anybody,\n")
    [m1] = await engine.run(
        OP_NAME, inputs=[t.id], speaker_db=db1, min_confidence=0.6
    )
    # Same path, different contents — speaker_db_sha changes, cache misses.
    db1.write_text("name,aliases\nKlaus Anybody,Mr Klaus\nJane Example,\n")
    [m2] = await engine.run(
        OP_NAME, inputs=[t.id], speaker_db=db1, min_confidence=0.6
    )
    assert m1.id != m2.id
