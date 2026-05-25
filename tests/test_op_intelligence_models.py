"""Curated intelligence-model dropdown — JSON Schema enum + permissive validation.

Mirrors ``test_op_audio_models``. The intelligence ops surface a curated
set of Gemini + Claude + mlx-community ids as a JSON Schema ``enum`` for
the Web UI, but the field type stays ``str`` so off-list ids still
validate (cache key is keyed on the model id, so we can't lock down).
"""

from __future__ import annotations

from media_engine.ops.intelligence._models import (
    CLAUDE_MODELS,
    GEMINI_MODELS,
    INTELLIGENCE_MODELS,
    MLX_LM_MODELS,
)
from media_engine.ops.intelligence.analyze import AnalyzeParams
from media_engine.ops.intelligence.classify import ClassifyParams
from media_engine.ops.intelligence.extract import ExtractParams
from media_engine.ops.intelligence.summarize import SummarizeParams


def test_catalog_is_non_empty_and_includes_default() -> None:
    assert len(GEMINI_MODELS) >= 3
    assert len(CLAUDE_MODELS) >= 1
    assert len(MLX_LM_MODELS) >= 1
    assert INTELLIGENCE_MODELS == GEMINI_MODELS + CLAUDE_MODELS + MLX_LM_MODELS
    # Every op's default must be a member of the catalog.
    for default in (
        SummarizeParams(focus=None).model,
        ClassifyParams(labels=["a"]).model,
        ExtractParams(prompt="x", schema_def={"type": "object"}).model,
        AnalyzeParams(prompt="x", schema_def={"type": "object"}).model,
    ):
        assert default in INTELLIGENCE_MODELS


def test_extract_schema_emits_intelligence_enum() -> None:
    schema = ExtractParams.model_json_schema()
    enum = schema["properties"]["model"].get("enum")
    assert enum is not None, "model field must carry a JSON Schema enum"
    assert tuple(enum) == INTELLIGENCE_MODELS


def test_summarize_schema_emits_intelligence_enum() -> None:
    schema = SummarizeParams.model_json_schema()
    assert tuple(schema["properties"]["model"]["enum"]) == INTELLIGENCE_MODELS


def test_classify_schema_emits_intelligence_enum() -> None:
    schema = ClassifyParams.model_json_schema()
    assert tuple(schema["properties"]["model"]["enum"]) == INTELLIGENCE_MODELS


def test_analyze_schema_emits_intelligence_enum() -> None:
    schema = AnalyzeParams.model_json_schema()
    enum = schema["properties"]["model"].get("enum")
    assert enum is not None
    assert tuple(enum) == INTELLIGENCE_MODELS


def test_off_list_models_still_validate() -> None:
    """The enum is a UI affordance, not a server-side constraint.

    A power user passing an off-list community fp32 build or a preview
    Gemini id via ``med run intelligence.summarize --param model=…`` must
    still construct a valid params object — otherwise we'd silently break
    cache-key reproducibility for anyone pinning a specific snapshot.
    """
    p = SummarizeParams(model="gemini-3-pro-preview")
    assert p.model == "gemini-3-pro-preview"
    p2 = ExtractParams(
        prompt="x",
        schema_def={"type": "object"},
        model="mlx-community/SomeFuture-13B-4bit",
    )
    assert p2.model == "mlx-community/SomeFuture-13B-4bit"
