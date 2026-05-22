"""Smoke tests for the bundled ``analysis-full`` profile.

Phase 5 commit 36 ships the profile files (YAML + prompt + schema +
speaker CSV + Jinja2 templates) and extends ``AnalyzeParams`` with
``prompt_path`` resolution. The full DAG compile + e2e render lands in
commit 37 (which adds ``report.session``/``report.zeitgeist``); here
we only validate the pieces that don't require those ops yet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from media_engine.ops.intelligence.analyze import AnalyzeParams
from media_engine.ops.speakers._speaker_db import load_speaker_db
from media_engine.profiles.loader import discover_profiles
from media_engine.profiles.schema import PipelineProfile
from media_engine.runtime.jsonschema import load_schema, validate

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = REPO_ROOT / "profiles" / "analysis-full"


# ─────────────────────────────────────────────────────────────────
# Profile files are present and parse cleanly
# ─────────────────────────────────────────────────────────────────


def test_profile_directory_exists() -> None:
    assert PROFILE_DIR.is_dir()
    for fname in (
        "analysis-full.yaml",
        "analyze_prompt.md",
        "analysis_schema.json",
        "speakers.csv",
        "session_report.md.j2",
        "zeitgeist_report.md.j2",
    ):
        assert (PROFILE_DIR / fname).is_file(), f"missing: {fname}"


def test_analysis_full_discovered_from_repo() -> None:
    """`<repo>/profiles/` is on the default discovery path, so
    `med profile ls` (and any caller of discover_profiles) sees it."""
    profiles = discover_profiles(repo_dir=REPO_ROOT / "profiles")
    assert "analysis-full" in profiles
    path, profile = profiles["analysis-full"]
    assert path == PROFILE_DIR / "analysis-full.yaml"
    assert isinstance(profile, PipelineProfile)
    assert profile.kind == "pipeline"


def test_pipeline_graph_node_ids() -> None:
    profiles = discover_profiles(repo_dir=REPO_ROOT / "profiles")
    _, profile = profiles["analysis-full"]
    assert isinstance(profile, PipelineProfile)
    ids = [n.id for n in profile.graph]
    assert ids == ["audio", "transcript", "identified", "analyzed", "report"]
    ops = [n.op for n in profile.graph]
    assert ops == [
        "video.extract_audio",
        "audio.transcribe_diarized",
        "speakers.identify",
        "intelligence.analyze",
        "report.session",
    ]
    assert profile.outputs == ["report"]


# ─────────────────────────────────────────────────────────────────
# Schema + prompt + speaker DB load cleanly
# ─────────────────────────────────────────────────────────────────


def test_analysis_schema_loads_and_accepts_valid_instance() -> None:
    schema = load_schema(PROFILE_DIR / "analysis_schema.json")
    valid = {
        "summary": "Speaker discussed supply chain risks.",
        "topics": ["supply chain", "logistics"],
        "entities": ["Acme Corp", "Port of Long Beach"],
        "claims": ["Shipping costs doubled in Q3."],
        "sentiment": {"polarity": -0.2, "confidence": 0.6},
        "questions": ["Will costs normalize next year?"],
    }
    validate(valid, schema)  # no exception → schema accepts


def test_analyze_prompt_md_nonempty() -> None:
    content = (PROFILE_DIR / "analyze_prompt.md").read_text(encoding="utf-8")
    assert len(content.strip()) > 200, "prompt file should be substantive"


def test_speakers_csv_loads() -> None:
    db = load_speaker_db(PROFILE_DIR / "speakers.csv")
    assert len(db) >= 1
    # Aliases column is parsed:
    for entry in db:
        assert entry.canonical
        assert len(entry.candidates) >= 1


def test_session_template_references_expected_variables() -> None:
    template = (PROFILE_DIR / "session_report.md.j2").read_text(encoding="utf-8")
    # The contract documented in docs/profile_analysis_full.md:
    for marker in ("segments", "speaker_names", "model", "backend"):
        assert marker in template, f"template missing reference to {marker!r}"


def test_zeitgeist_template_references_expected_variables() -> None:
    template = (PROFILE_DIR / "zeitgeist_report.md.j2").read_text(encoding="utf-8")
    for marker in ("aggregate", "sessions", "top_topics", "top_entities"):
        assert marker in template, f"template missing reference to {marker!r}"


# ─────────────────────────────────────────────────────────────────
# AnalyzeParams.prompt_path resolution
# ─────────────────────────────────────────────────────────────────


def test_analyze_params_prompt_path_resolves_to_inline_prompt(
    tmp_path: Path,
) -> None:
    p = tmp_path / "prompt.md"
    p.write_text("Analyze this carefully.\n", encoding="utf-8")
    params = AnalyzeParams(
        prompt_path=p,  # type: ignore[call-arg]  # not a real field, validated by mode=before
        schema_def={"type": "object"},
    )
    assert params.prompt == "Analyze this carefully.\n"
    # prompt_path was popped; it shouldn't appear in dumped params
    # (cache key must reflect resolved text, not file path).
    dumped = params.model_dump()
    assert "prompt_path" not in dumped
    assert dumped["prompt"] == "Analyze this carefully.\n"


def test_analyze_params_prompt_path_cache_key_tracks_text(
    tmp_path: Path,
) -> None:
    """Two prompt_path inputs pointing to files with the same content
    produce identical canonical params (cache hit). Different content
    produces different canonical params (cache miss)."""
    p1 = tmp_path / "a.md"
    p2 = tmp_path / "b.md"
    p1.write_text("identical", encoding="utf-8")
    p2.write_text("identical", encoding="utf-8")
    a = AnalyzeParams(prompt_path=p1, schema_def={"type": "object"})  # type: ignore[call-arg]
    b = AnalyzeParams(prompt_path=p2, schema_def={"type": "object"})  # type: ignore[call-arg]
    assert a.model_dump() == b.model_dump()

    p2.write_text("different", encoding="utf-8")
    c = AnalyzeParams(prompt_path=p2, schema_def={"type": "object"})  # type: ignore[call-arg]
    assert a.model_dump() != c.model_dump()


def test_analyze_params_requires_prompt_or_prompt_path() -> None:
    with pytest.raises(ValueError, match="prompt"):
        AnalyzeParams(schema_def={"type": "object"})


def test_analyze_params_inline_prompt_still_works() -> None:
    params = AnalyzeParams(prompt="hi", schema_def={"type": "object"})
    assert params.prompt == "hi"
