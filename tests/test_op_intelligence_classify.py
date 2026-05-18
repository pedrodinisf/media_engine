"""Tests for ops/intelligence/classify.py (thin wrapper over extract)."""

from __future__ import annotations

import importlib.util
import os

import pytest
from pydantic import ValidationError

from media_engine.artifacts import Analysis, Kind
from media_engine.ops.intelligence.classify import (
    ClassifyParams,
    IntelligenceClassify,
)
from media_engine.runtime.engine import Engine

from ._intel import (
    make_transcript,
    register_fake_extract_backend,
    unregister_fake,
)


def _spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


GENAI_AVAILABLE = _spec("google.genai")


@pytest.fixture
def fake_extract():
    cls = register_fake_extract_backend()
    yield cls
    unregister_fake()


def test_op_class_attributes() -> None:
    assert IntelligenceClassify.name == "intelligence.classify"
    assert IntelligenceClassify.variadic_inputs is True
    assert IntelligenceClassify.output_kinds == (Kind.Analysis,)
    assert IntelligenceClassify.default_backend is None


def test_params_require_labels() -> None:
    with pytest.raises(ValidationError):
        ClassifyParams(labels=[])
    p = ClassifyParams(labels=["finance", "tech"])
    assert p.multi_label is False


async def test_classify_via_fake_backend(
    engine: Engine, fake_extract
) -> None:
    t = make_transcript(engine, "the merger will reshape the tech sector")
    [analysis] = await engine.run(
        "intelligence.classify",
        inputs=[t.id],
        labels=["finance", "technology", "sports"],
    )
    assert isinstance(analysis, Analysis)
    assert set(analysis.data) == {"labels", "confidence", "rationale"}
    assert isinstance(analysis.data["confidence"], dict)


async def test_classify_cache_hit(
    engine: Engine, fake_extract, mocker
) -> None:
    t = make_transcript(engine)
    [a1] = await engine.run(
        "intelligence.classify", inputs=[t.id], labels=["a", "b"]
    )
    spy = mocker.spy(fake_extract, "execute")
    [a2] = await engine.run(
        "intelligence.classify", inputs=[t.id], labels=["a", "b"]
    )
    assert spy.call_count == 0
    assert a1.id == a2.id


async def test_classify_label_change_new_id(
    engine: Engine, fake_extract
) -> None:
    t = make_transcript(engine)
    [a] = await engine.run(
        "intelligence.classify", inputs=[t.id], labels=["a", "b"]
    )
    [b] = await engine.run(
        "intelligence.classify", inputs=[t.id], labels=["a", "c"]
    )
    assert a.id != b.id


def test_cost_estimate_delegates() -> None:
    est = IntelligenceClassify().cost_estimate(
        [], ClassifyParams(labels=["x"])
    )
    assert est.cloud_cents > 0


@pytest.mark.needs_gemini
@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai not installed")
async def test_real_gemini_smoke(engine: Engine) -> None:
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    t = make_transcript(
        engine, "Quarterly earnings beat expectations; the stock rallied."
    )
    [a] = await engine.run(
        "intelligence.classify",
        inputs=[t.id],
        labels=["finance", "technology", "politics"],
        model="gemini-2.5-flash",
    )
    assert isinstance(a, Analysis)
    assert isinstance(a.data["labels"], list)
    assert isinstance(a.data["rationale"], str)
