"""Regression — Params models guard numeric fields with ge/le.

Audit findings F-016..F-019. The Web UI's auto-form generator reads
``json_schema_extra`` + Pydantic constraints to render guard rails on
numeric inputs. Without ``ge``/``le``, the form can't enforce sane
ranges and the backend accepts nonsense like ``temperature=99`` or
``top_k=0``.

Reference (already-shipped good example): ``video.comprehend``'s
``ComprehendParams`` uses ``Field(ge=0.0, le=2.0)`` on temperature
and ``Field(ge=1, le=32768)`` on max_tokens.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from media_engine.ops.intelligence.analyze import AnalyzeParams
from media_engine.ops.intelligence.classify import ClassifyParams
from media_engine.ops.intelligence.summarize import SummarizeParams
from media_engine.ops.search.hybrid import SearchHybridParams


def test_classify_temperature_bounds() -> None:
    """F-016 — temperature must be in [0.0, 2.0]."""
    with pytest.raises(ValidationError):
        ClassifyParams(labels=["a"], temperature=99.0)
    with pytest.raises(ValidationError):
        ClassifyParams(labels=["a"], temperature=-1.0)
    # In range succeeds.
    ClassifyParams(labels=["a"], temperature=0.7)


def test_classify_max_tokens_bounds() -> None:
    """F-016 — max_tokens must be >= 1."""
    with pytest.raises(ValidationError):
        ClassifyParams(labels=["a"], max_tokens=0)
    with pytest.raises(ValidationError):
        ClassifyParams(labels=["a"], max_tokens=-100)
    ClassifyParams(labels=["a"], max_tokens=512)


def test_summarize_temperature_bounds() -> None:
    """F-017 — temperature must be in [0.0, 2.0]."""
    with pytest.raises(ValidationError):
        SummarizeParams(temperature=3.0)
    SummarizeParams(temperature=0.5)


def test_summarize_max_tokens_bounds() -> None:
    """F-017 — max_tokens must be >= 1."""
    with pytest.raises(ValidationError):
        SummarizeParams(max_tokens=0)
    SummarizeParams(max_tokens=1024)


def test_analyze_temperature_bounds() -> None:
    """F-018 — temperature must be in [0.0, 2.0]."""
    with pytest.raises(ValidationError):
        AnalyzeParams(prompt="x", schema_def={}, temperature=10.0)
    AnalyzeParams(prompt="x", schema_def={}, temperature=0.2)


def test_analyze_max_tokens_bounds() -> None:
    """F-018 — max_tokens must be >= 1."""
    with pytest.raises(ValidationError):
        AnalyzeParams(prompt="x", schema_def={}, max_tokens=0)
    AnalyzeParams(prompt="x", schema_def={}, max_tokens=2048)


def test_analyze_window_bounds() -> None:
    """F-018 — window must be >= 1 (Field(ge=1) replaces former field_validator)."""
    with pytest.raises(ValidationError):
        AnalyzeParams(prompt="x", schema_def={}, window=0)
    AnalyzeParams(prompt="x", schema_def={}, window=3)


def test_hybrid_top_k_bounds() -> None:
    """F-019 — top_k must be in [1, 1000]."""
    with pytest.raises(ValidationError):
        SearchHybridParams(query="q", top_k=0)
    with pytest.raises(ValidationError):
        SearchHybridParams(query="q", top_k=1001)
    SearchHybridParams(query="q", top_k=10)


def test_hybrid_rrf_k_bounds() -> None:
    """F-019 — rrf_k must be >= 1."""
    with pytest.raises(ValidationError):
        SearchHybridParams(query="q", rrf_k=0)
    SearchHybridParams(query="q", rrf_k=60)
