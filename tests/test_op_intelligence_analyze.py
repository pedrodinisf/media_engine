"""Tests for ops/intelligence/analyze.py (composite, per-window)."""

from __future__ import annotations

import importlib.util

import pytest

from media_engine.artifacts import Kind, SessionAnalysis
from media_engine.ops.intelligence.analyze import (
    AnalyzeParams,
    IntelligenceAnalyze,
    _windows,
)
from media_engine.runtime.engine import Engine
from media_engine.runtime.jsonschema import SchemaError

from ._intel import register_fake_extract_backend, unregister_fake


def _spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


GENAI_AVAILABLE = _spec("google.genai")

_SCHEMA = {
    "type": "object",
    "properties": {"sentiment": {"type": "string"}},
    "required": ["sentiment"],
    "additionalProperties": False,
}


@pytest.fixture
def fake_extract():
    cls = register_fake_extract_backend()
    yield cls
    unregister_fake()


def _transcript(engine: Engine, n: int) -> str:
    segs = [
        {"start": float(i), "end": float(i + 1), "text": f"sentence {i}",
         "speaker": f"S{i % 2}"}
        for i in range(n)
    ]
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from media_engine.artifacts import Transcript

    tid = "a" * 64
    payload = {"text": " ".join(s["text"] for s in segs), "segments": segs}
    p = engine.storage.artifact_path(tid, ".json")
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(payload))
    t = Transcript(id=tid, path=p, metadata=payload,
                    created_at=datetime.now(UTC))
    engine.cache.upsert_artifact(t)
    return t.id


def test_op_class_attributes() -> None:
    assert IntelligenceAnalyze.name == "intelligence.analyze"
    assert IntelligenceAnalyze.input_kinds == (Kind.Transcript,)
    assert IntelligenceAnalyze.output_kinds == (Kind.SessionAnalysis,)
    assert IntelligenceAnalyze.default_backend is None


def test_windows_grouping() -> None:
    segs = [{"i": i} for i in range(5)]
    assert len(_windows(segs, 2)) == 3
    assert _windows(segs, 2)[-1] == [{"i": 4}]
    assert len(_windows(segs, 1)) == 5


def test_params_window_validation() -> None:
    with pytest.raises(ValueError, match="window must be >= 1"):
        AnalyzeParams(prompt="p", schema_def=_SCHEMA, window=0)


async def test_analyze_per_segment(
    engine: Engine, fake_extract
) -> None:
    tid = _transcript(engine, 4)
    [sa] = await engine.run(
        "intelligence.analyze",
        inputs=[tid],
        prompt="Rate the sentiment.",
        schema_def=_SCHEMA,
        window=2,
    )
    assert isinstance(sa, SessionAnalysis)
    assert sa.kind == Kind.SessionAnalysis
    data = sa.metadata["data"]
    assert len(data) == 2  # 4 segments / window 2
    assert data[0]["analysis"] == {"sentiment": "x"}
    assert data[0]["speaker"] == "S0"
    assert sa.metadata["segment_count"] == 4
    assert sa.metadata["usage"]["input_tokens"] == 2 * 500


async def test_analyze_with_classification(
    engine: Engine, fake_extract
) -> None:
    tid = _transcript(engine, 2)
    [sa] = await engine.run(
        "intelligence.analyze",
        inputs=[tid],
        prompt="Analyze.",
        schema_def=_SCHEMA,
        window=1,
        classify_labels=["positive", "negative"],
    )
    data = sa.metadata["data"]
    assert len(data) == 2
    assert set(data[0]["classification"]) == {
        "labels", "confidence", "rationale"
    }
    # extract + classify pass per window → 2 usage rows each.
    assert sa.metadata["usage"]["input_tokens"] == 2 * 2 * 500


async def test_analyze_cache_hit(
    engine: Engine, fake_extract, mocker
) -> None:
    tid = _transcript(engine, 3)
    kw = {"prompt": "p", "schema_def": _SCHEMA}
    [a1] = await engine.run("intelligence.analyze", inputs=[tid], **kw)
    spy = mocker.spy(fake_extract, "execute")
    [a2] = await engine.run("intelligence.analyze", inputs=[tid], **kw)
    assert spy.call_count == 0
    assert a1.id == a2.id


async def test_analyze_bad_schema_fails_fast(
    engine: Engine, fake_extract
) -> None:
    tid = _transcript(engine, 1)
    with pytest.raises(SchemaError):
        await engine.run(
            "intelligence.analyze", inputs=[tid], prompt="p",
            schema_def="/no/such.json",
        )


async def test_analyze_rejects_empty_transcript(
    engine: Engine, fake_extract
) -> None:
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from media_engine.artifacts import Transcript

    tid = "e" * 64
    p = engine.storage.artifact_path(tid, ".json")
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps({"text": "", "segments": []}))
    empty = Transcript(
        id=tid, path=p, metadata={"text": "", "segments": []},
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(empty)
    with pytest.raises(ValueError, match="no segments"):
        await engine.run(
            "intelligence.analyze", inputs=[tid], prompt="p",
            schema_def=_SCHEMA,
        )


def test_cost_estimate_scales_with_windows(engine: Engine) -> None:
    tid = _transcript(engine, 10)
    t = engine.get_artifact(tid)
    op = IntelligenceAnalyze()
    one = op.cost_estimate(
        [t], AnalyzeParams(prompt="p", schema_def=_SCHEMA, window=10)
    )
    many = op.cost_estimate(
        [t], AnalyzeParams(prompt="p", schema_def=_SCHEMA, window=1)
    )
    assert many.cloud_cents > one.cloud_cents


@pytest.mark.needs_gemini
@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai not installed")
async def test_real_gemini_smoke(engine: Engine) -> None:
    import os

    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    tid = _transcript(engine, 2)
    [sa] = await engine.run(
        "intelligence.analyze",
        inputs=[tid],
        prompt="Give a one-word sentiment for this segment.",
        schema_def=_SCHEMA,
        window=1,
        model="gemini-2.5-flash",
    )
    assert isinstance(sa, SessionAnalysis)
    assert len(sa.metadata["data"]) == 2
