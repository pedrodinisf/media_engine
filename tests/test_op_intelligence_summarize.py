"""Tests for ops/intelligence/summarize.py (thin wrapper over extract)."""

from __future__ import annotations

import importlib.util
import os

import pytest

from media_engine.artifacts import Analysis, Kind
from media_engine.ops.intelligence.summarize import (
    IntelligenceSummarize,
    SummarizeParams,
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
    assert IntelligenceSummarize.name == "intelligence.summarize"
    assert IntelligenceSummarize.variadic_inputs is True
    assert IntelligenceSummarize.output_kinds == (Kind.Analysis,)
    assert IntelligenceSummarize.default_backend is None


def test_params_defaults() -> None:
    p = SummarizeParams()
    assert p.model == "gemini-2.5-flash"
    assert p.focus is None


async def test_summarize_via_fake_backend(
    engine: Engine, fake_extract
) -> None:
    t = make_transcript(engine, "long document about quarterly results")
    [analysis] = await engine.run("intelligence.summarize", inputs=[t.id])
    assert isinstance(analysis, Analysis)
    assert set(analysis.data) == {"summary", "key_points"}
    assert isinstance(analysis.data["key_points"], list)


async def test_summarize_cache_hit(
    engine: Engine, fake_extract, mocker
) -> None:
    t = make_transcript(engine)
    [a1] = await engine.run("intelligence.summarize", inputs=[t.id])
    spy = mocker.spy(fake_extract, "execute")
    [a2] = await engine.run("intelligence.summarize", inputs=[t.id])
    assert spy.call_count == 0
    assert a1.id == a2.id


async def test_summarize_focus_changes_id(
    engine: Engine, fake_extract
) -> None:
    t = make_transcript(engine)
    [a] = await engine.run("intelligence.summarize", inputs=[t.id])
    [b] = await engine.run(
        "intelligence.summarize", inputs=[t.id], focus="risks"
    )
    assert a.id != b.id


async def test_summarize_rejects_no_input(
    engine: Engine, fake_extract
) -> None:
    with pytest.raises(ValueError, match="expects ≥1 input"):
        await engine.run("intelligence.summarize", inputs=[])


def test_cost_estimate_delegates() -> None:
    est = IntelligenceSummarize().cost_estimate([], SummarizeParams())
    assert est.cloud_cents > 0


@pytest.mark.needs_gemini
@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai not installed")
async def test_real_gemini_smoke(engine: Engine) -> None:
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    t = make_transcript(
        engine,
        "The board met to review hiring, the budget, and a new product "
        "line. They approved the budget and deferred hiring decisions.",
    )
    [a] = await engine.run(
        "intelligence.summarize", inputs=[t.id], model="gemini-2.5-flash"
    )
    assert isinstance(a, Analysis)
    assert isinstance(a.data["summary"], str) and a.data["summary"]
    assert isinstance(a.data["key_points"], list)
