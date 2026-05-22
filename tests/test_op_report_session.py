"""Tests for ops/report/session.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from media_engine.artifacts import (
    Kind,
    MarkdownArtifact,
    SessionAnalysis,
    Transcript,
    compute_derived_artifact_id,
)
from media_engine.ops.report.session import (
    OP_NAME,
    ReportSession,
    SessionReportParams,
)
from media_engine.runtime.engine import Engine

# ─────────────────────────────────────────────────────────────────
# Op class invariants
# ─────────────────────────────────────────────────────────────────


def test_op_class_attributes() -> None:
    assert ReportSession.name == "report.session"
    assert ReportSession.input_kinds == (Kind.SessionAnalysis,)
    assert ReportSession.output_kinds == (Kind.MarkdownArtifact,)
    assert ReportSession.default_backend is None
    assert ReportSession.variadic_inputs is False
    assert "backend" not in SessionReportParams.model_fields


def test_params_template_sha_auto_populated(tmp_path: Path) -> None:
    tpl = tmp_path / "t.md.j2"
    tpl.write_text("# {{ title }}", encoding="utf-8")
    p = SessionReportParams(template=tpl)
    assert len(p.template_sha) == 16


def test_params_template_sha_marks_missing(tmp_path: Path) -> None:
    p = SessionReportParams(template=tmp_path / "nope.j2")
    assert p.template_sha == "missing"


# ─────────────────────────────────────────────────────────────────
# Synthetic SessionAnalysis fixture
# ─────────────────────────────────────────────────────────────────


def _make_session_analysis(engine: Engine, salt: str = "default") -> SessionAnalysis:
    """Persist a synthetic SessionAnalysis artifact mirroring what
    intelligence.analyze would emit. Includes speaker_names so the
    template's speakers section renders."""
    data: list[dict[str, Any]] = [
        {
            "window_index": 0,
            "start": 0.0,
            "end": 30.0,
            "text": "Hello everyone.",
            "speaker": "SPEAKER_00",
            "analysis": {
                "summary": "Speaker introduces the agenda.",
                "topics": ["agenda"],
                "entities": ["Acme Corp"],
                "claims": ["The agenda has three items."],
                "sentiment": {"polarity": 0.1, "confidence": 0.7},
                "questions": [],
            },
        },
        {
            "window_index": 1,
            "start": 30.0,
            "end": 60.0,
            "text": "Now the second topic.",
            "speaker": "SPEAKER_01",
            "analysis": {
                "summary": "Discussion moves to the second topic.",
                "topics": ["logistics"],
                "entities": [],
                "claims": [],
                "sentiment": {"polarity": -0.1, "confidence": 0.5},
                "questions": ["What is the timeline?"],
            },
        },
    ]
    payload: dict[str, Any] = {
        "data": data,
        "model": "test:fake",
        "backend": "fake",
        "window": 1,
        "segment_count": 2,
        "speaker_names": {"SPEAKER_00": "Alex Example", "SPEAKER_01": None},
        "usage": {"input_tokens": 0, "output_tokens": 0, "cost_cents": 0.0},
    }
    derived_id = compute_derived_artifact_id(
        kind=Kind.SessionAnalysis,
        op_name="test.synth",
        op_version="1",
        backend_name=None,
        backend_version=None,
        params={"salt": salt},
        input_ids=[],
    )
    tmp = engine.storage.ensure_workdir("report-session-test") / "sa.json"
    tmp.write_text(json.dumps(payload))
    dest = engine.storage.store_file(tmp, derived_id, ".json")
    sa = SessionAnalysis(
        id=derived_id,
        path=dest,
        metadata=payload,
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(sa)
    return sa


def _simple_template(tmp_path: Path) -> Path:
    """A minimal template that touches every documented context variable
    so we can assert downstream against expected substrings."""
    p = tmp_path / "session.md.j2"
    p.write_text(
        (
            "# {{ title or 'Untitled' }}\n"
            "Model: {{ model }}\n"
            "Backend: {{ backend }}\n"
            "Speakers: {{ speaker_names | length }}\n"
            "{% for w in segments -%}\n"
            "## Window {{ w.window_index }}\n"
            "{{ w.analysis.summary }}\n"
            "{% endfor -%}\n"
        ),
        encoding="utf-8",
    )
    return p


# ─────────────────────────────────────────────────────────────────
# Engine-driven success / cache / param-change
# ─────────────────────────────────────────────────────────────────


async def test_render_produces_markdown_artifact(
    engine: Engine, tmp_path: Path
) -> None:
    sa = _make_session_analysis(engine)
    tpl = _simple_template(tmp_path)
    [out] = await engine.run(
        OP_NAME, inputs=[sa.id], template=tpl, title="My Session"
    )
    assert isinstance(out, MarkdownArtifact)
    text = Path(out.path).read_text(encoding="utf-8")
    assert "# My Session" in text
    assert "Window 0" in text
    assert "Window 1" in text
    assert "Speaker introduces the agenda" in text
    assert out.metadata["n_segments"] == 2


async def test_cache_hit_on_rerun(
    engine: Engine, tmp_path: Path, mocker: Any
) -> None:
    sa = _make_session_analysis(engine)
    tpl = _simple_template(tmp_path)
    [m1] = await engine.run(OP_NAME, inputs=[sa.id], template=tpl)
    spy = mocker.spy(ReportSession, "run")
    [m2] = await engine.run(OP_NAME, inputs=[sa.id], template=tpl)
    assert spy.call_count == 0
    assert m1.id == m2.id


async def test_cache_miss_on_template_edit(
    engine: Engine, tmp_path: Path
) -> None:
    sa = _make_session_analysis(engine)
    tpl = _simple_template(tmp_path)
    [m1] = await engine.run(OP_NAME, inputs=[sa.id], template=tpl)
    # Edit the template file — same path, different bytes.
    tpl.write_text("# Edited\n{{ segments | length }}", encoding="utf-8")
    [m2] = await engine.run(OP_NAME, inputs=[sa.id], template=tpl)
    assert m1.id != m2.id


async def test_cache_miss_on_title_change(
    engine: Engine, tmp_path: Path
) -> None:
    sa = _make_session_analysis(engine)
    tpl = _simple_template(tmp_path)
    [m1] = await engine.run(OP_NAME, inputs=[sa.id], template=tpl, title="A")
    [m2] = await engine.run(OP_NAME, inputs=[sa.id], template=tpl, title="B")
    assert m1.id != m2.id


async def test_missing_template_errors_with_path(
    engine: Engine, tmp_path: Path
) -> None:
    sa = _make_session_analysis(engine)
    missing = tmp_path / "nope.j2"
    with pytest.raises(FileNotFoundError, match="template not found"):
        await engine.run(OP_NAME, inputs=[sa.id], template=missing)


async def test_rejects_wrong_kind_input(
    engine: Engine, tmp_path: Path
) -> None:
    # Build a Transcript instead of a SessionAnalysis.
    seg = [{"start": 0.0, "end": 1.0, "text": "hi"}]
    payload = {"text": "hi", "segments": seg}
    tid = compute_derived_artifact_id(
        kind=Kind.Transcript,
        op_name="test.synth",
        op_version="1",
        backend_name=None,
        backend_version=None,
        params={"k": "v"},
        input_ids=[],
    )
    p = engine.storage.ensure_workdir("wrong") / "t.json"
    p.write_text(json.dumps(payload))
    dest = engine.storage.store_file(p, tid, ".json")
    t = Transcript(
        id=tid, path=dest, metadata=payload, created_at=datetime.now(UTC)
    )
    engine.cache.upsert_artifact(t)

    tpl = _simple_template(tmp_path)
    with pytest.raises(ValueError, match="kind mismatch"):
        await engine.run(OP_NAME, inputs=[t.id], template=tpl)


async def test_extra_context_merged_into_template(
    engine: Engine, tmp_path: Path
) -> None:
    sa = _make_session_analysis(engine)
    tpl = tmp_path / "extra.j2"
    tpl.write_text("X={{ flavor }}", encoding="utf-8")
    [out] = await engine.run(
        OP_NAME,
        inputs=[sa.id],
        template=tpl,
        extra_context={"flavor": "strawberry"},
    )
    assert Path(out.path).read_text(encoding="utf-8") == "X=strawberry"


async def test_cache_hits_across_template_paths_with_same_content(
    engine: Engine, tmp_path: Path
) -> None:
    """Two template files with identical content at different paths
    share the cache key — ``template`` is ``exclude=True`` so only
    ``template_sha`` enters canonical params."""
    sa = _make_session_analysis(engine)
    body = "# {{ title or 'A' }}\n{{ segments | length }}"
    a = tmp_path / "a.md.j2"
    b = tmp_path / "b.md.j2"
    a.write_text(body, encoding="utf-8")
    b.write_text(body, encoding="utf-8")
    [m1] = await engine.run(OP_NAME, inputs=[sa.id], template=a)
    [m2] = await engine.run(OP_NAME, inputs=[sa.id], template=b)
    assert m1.id == m2.id


def test_template_excluded_from_canonical_params(tmp_path: Path) -> None:
    tpl = tmp_path / "t.j2"
    tpl.write_text("x", encoding="utf-8")
    p = SessionReportParams(template=tpl)
    dumped = p.model_dump()
    assert "template" not in dumped, dumped
    assert dumped.get("template_sha"), dumped
    # Still accessible on the model for the op's run() path.
    assert p.template == tpl
