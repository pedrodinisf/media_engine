"""Tests for ops/report/zeitgeist.py."""

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
    compute_derived_artifact_id,
)
from media_engine.ops.report.zeitgeist import (
    OP_NAME,
    ReportZeitgeist,
    ZeitgeistReportParams,
    _aggregate,
)
from media_engine.runtime.engine import Engine

# ─────────────────────────────────────────────────────────────────
# Op class invariants
# ─────────────────────────────────────────────────────────────────


def test_op_class_attributes() -> None:
    assert ReportZeitgeist.name == "report.zeitgeist"
    assert ReportZeitgeist.input_kinds == (Kind.SessionAnalysis,)
    assert ReportZeitgeist.variadic_inputs is True
    assert ReportZeitgeist.output_kinds == (Kind.MarkdownArtifact,)
    assert ReportZeitgeist.default_backend is None
    assert "backend" not in ZeitgeistReportParams.model_fields


# ─────────────────────────────────────────────────────────────────
# _aggregate pure-function
# ─────────────────────────────────────────────────────────────────


def _stub_session(windows: list[dict[str, Any]]) -> SessionAnalysis:
    """Build an unpersisted SessionAnalysis with only metadata.data filled —
    enough for _aggregate to read."""
    return SessionAnalysis(
        id="f" * 64,
        path=Path("/tmp/stub.json"),
        metadata={"data": windows},
        created_at=datetime.now(UTC),
    )


def test_aggregate_counts_topics_entities_claims_by_frequency() -> None:
    s1 = _stub_session([
        {
            "speaker": "Alex",
            "analysis": {
                "topics": ["alpha", "beta"],
                "entities": ["Acme", "Beta Corp"],
                "claims": ["thing happens"],
                "sentiment": {"polarity": 0.5, "confidence": 0.8},
            },
        },
        {
            "speaker": "Alex",
            "analysis": {
                "topics": ["alpha"],
                "entities": ["Acme"],
                "claims": ["thing happens"],
                "sentiment": {"polarity": 0.3, "confidence": 0.6},
            },
        },
    ])
    s2 = _stub_session([
        {
            "speaker": "Sam",
            "analysis": {
                "topics": ["beta", "gamma"],
                "entities": ["Beta Corp"],
                "claims": ["another claim"],
                "sentiment": {"polarity": -0.2, "confidence": 0.4},
            },
        },
    ])
    params = ZeitgeistReportParams(template=Path("/tmp/x.j2"))
    agg = _aggregate([s1, s2], params)
    topics = dict(agg["top_topics"])
    assert topics["alpha"] == 2
    assert topics["beta"] == 2
    assert topics["gamma"] == 1
    entities = dict(agg["top_entities"])
    assert entities["Acme"] == 2
    assert entities["Beta Corp"] == 2
    claims = dict(agg["top_claims"])
    assert claims["thing happens"] == 2
    speakers = dict(agg["top_speakers"])
    assert speakers["Alex"] == 2
    assert speakers["Sam"] == 1
    assert agg["polarity_count"] == 3
    assert abs(agg["avg_polarity"] - (0.5 + 0.3 - 0.2) / 3) < 1e-9
    assert agg["n_sessions"] == 2
    assert agg["n_windows"] == 3


def test_aggregate_empty_polarities_yields_none() -> None:
    s = _stub_session([{"analysis": {"topics": []}}])
    params = ZeitgeistReportParams(template=Path("/tmp/x.j2"))
    agg = _aggregate([s], params)
    assert agg["avg_polarity"] is None
    assert agg["polarity_count"] == 0


def test_aggregate_respects_top_n_params() -> None:
    windows = [
        {"analysis": {"topics": [f"t{i}"]}} for i in range(50)
    ]
    s = _stub_session(windows)
    params = ZeitgeistReportParams(
        template=Path("/tmp/x.j2"), top_n_topics=5
    )
    agg = _aggregate([s], params)
    assert len(agg["top_topics"]) == 5


# ─────────────────────────────────────────────────────────────────
# Engine-driven success / cache
# ─────────────────────────────────────────────────────────────────


def _persist_session(
    engine: Engine, salt: str, windows: list[dict[str, Any]]
) -> SessionAnalysis:
    payload = {
        "data": windows,
        "model": "test:fake",
        "backend": "fake",
        "window": 1,
        "segment_count": len(windows),
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
    tmp = engine.storage.ensure_workdir("zeit-test") / f"sa-{salt}.json"
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


def _zeitgeist_template(tmp_path: Path) -> Path:
    p = tmp_path / "zeit.md.j2"
    p.write_text(
        (
            "# {{ title or 'Zeitgeist' }}\n"
            "Sessions: {{ aggregate.n_sessions }}\n"
            "Top topics:\n"
            "{% for t, c in aggregate.top_topics -%}\n"
            "- {{ t }} ({{ c }})\n"
            "{% endfor -%}\n"
        ),
        encoding="utf-8",
    )
    return p


async def test_zeitgeist_renders_across_sessions(
    engine: Engine, tmp_path: Path
) -> None:
    s1 = _persist_session(engine, "a", [
        {"analysis": {
            "topics": ["alpha"],
            "sentiment": {"polarity": 0.0, "confidence": 1.0},
        }},
    ])
    s2 = _persist_session(engine, "b", [
        {"analysis": {
            "topics": ["alpha", "beta"],
            "sentiment": {"polarity": 1.0, "confidence": 1.0},
        }},
    ])
    tpl = _zeitgeist_template(tmp_path)
    [out] = await engine.run(
        OP_NAME, inputs=[s1.id, s2.id], template=tpl, title="Q4 Trends"
    )
    assert isinstance(out, MarkdownArtifact)
    text = Path(out.path).read_text(encoding="utf-8")
    assert "Q4 Trends" in text
    assert "alpha (2)" in text
    assert "beta (1)" in text


async def test_zeitgeist_cache_hit_on_rerun(
    engine: Engine, tmp_path: Path, mocker: Any
) -> None:
    s1 = _persist_session(engine, "a", [{"analysis": {"topics": ["x"]}}])
    s2 = _persist_session(engine, "b", [{"analysis": {"topics": ["x"]}}])
    tpl = _zeitgeist_template(tmp_path)
    [m1] = await engine.run(OP_NAME, inputs=[s1.id, s2.id], template=tpl)
    spy = mocker.spy(ReportZeitgeist, "run")
    [m2] = await engine.run(OP_NAME, inputs=[s1.id, s2.id], template=tpl)
    assert spy.call_count == 0
    assert m1.id == m2.id


async def test_zeitgeist_cache_miss_on_param_change(
    engine: Engine, tmp_path: Path
) -> None:
    s1 = _persist_session(engine, "a", [{"analysis": {"topics": ["x"]}}])
    s2 = _persist_session(engine, "b", [{"analysis": {"topics": ["x"]}}])
    tpl = _zeitgeist_template(tmp_path)
    [m1] = await engine.run(
        OP_NAME, inputs=[s1.id, s2.id], template=tpl, top_n_topics=20
    )
    [m2] = await engine.run(
        OP_NAME, inputs=[s1.id, s2.id], template=tpl, top_n_topics=5
    )
    assert m1.id != m2.id


async def test_zeitgeist_one_session_still_renders(
    engine: Engine, tmp_path: Path
) -> None:
    s = _persist_session(engine, "solo", [{"analysis": {"topics": ["t"]}}])
    tpl = _zeitgeist_template(tmp_path)
    [out] = await engine.run(OP_NAME, inputs=[s.id], template=tpl)
    assert isinstance(out, MarkdownArtifact)
    text = Path(out.path).read_text(encoding="utf-8")
    assert "Sessions: 1" in text


async def test_zeitgeist_missing_template_errors(
    engine: Engine, tmp_path: Path
) -> None:
    s = _persist_session(engine, "x", [{"analysis": {"topics": ["t"]}}])
    with pytest.raises(FileNotFoundError, match="template not found"):
        await engine.run(OP_NAME, inputs=[s.id], template=tmp_path / "nope.j2")


def test_params_template_sha_auto_populated(tmp_path: Path) -> None:
    tpl = tmp_path / "t.j2"
    tpl.write_text("x", encoding="utf-8")
    p = ZeitgeistReportParams(template=tpl)
    assert len(p.template_sha) == 16
