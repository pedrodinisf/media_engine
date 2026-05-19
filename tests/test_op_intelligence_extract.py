"""Tests for ops/intelligence/extract.py + its backends.

Dispatch + parse/validate use a fake backend (always run). Real backend
smokes are gated: gemini (needs_gemini), claude (ANTHROPIC_API_KEY +
anthropic installed), mlx-lm (mlx_lm installed).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from media_engine.artifacts import Analysis, Kind
from media_engine.ops import OperationContext
from media_engine.ops.intelligence.extract import (
    ExtractParams,
    IntelligenceExtract,
    _default_backend_for_model,
    artifact_to_text,
    build_extract_analysis,
    parse_json_object,
)
from media_engine.runtime.engine import Engine
from media_engine.runtime.jsonschema import SchemaError

from ._intel import (
    ctx_for,
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
ANTHROPIC_AVAILABLE = importlib.util.find_spec("anthropic") is not None
MLX_LM_AVAILABLE = importlib.util.find_spec("mlx_lm") is not None

_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["topic", "entities"],
    "additionalProperties": False,
}


@pytest.fixture
def fake_extract():
    cls = register_fake_extract_backend()
    yield cls
    unregister_fake()


def test_op_class_attributes() -> None:
    assert IntelligenceExtract.name == "intelligence.extract"
    assert IntelligenceExtract.input_kinds == (
        Kind.Transcript, Kind.MarkdownArtifact, Kind.Analysis
    )
    assert IntelligenceExtract.variadic_inputs is True
    assert IntelligenceExtract.output_kinds == (Kind.Analysis,)
    assert IntelligenceExtract.default_backend == "gemini"


def test_backend_for_model() -> None:
    assert _default_backend_for_model("gemini-2.5-flash") == "gemini"
    assert _default_backend_for_model("claude-haiku-4") == "claude"
    assert (
        _default_backend_for_model("mlx-community/Qwen2.5-7B-Instruct-4bit")
        == "mlx-lm"
    )


def test_parse_json_object_strips_fences() -> None:
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('noise {"a": 2} trailing') == {"a": 2}


def test_parse_json_object_rejects_non_object() -> None:
    with pytest.raises(SchemaError, match="no JSON object"):
        parse_json_object("just prose, no json")


def test_artifact_to_text_transcript(engine: Engine) -> None:
    t = make_transcript(engine, "the meeting covered budgets")
    assert "budgets" in artifact_to_text(t)


async def test_extract_via_fake_backend(
    engine: Engine, fake_extract
) -> None:
    t = make_transcript(engine)
    [analysis] = await engine.run(
        "intelligence.extract",
        inputs=[t.id],
        prompt="Pull the topic and entities.",
        schema_def=_SCHEMA,
    )
    assert isinstance(analysis, Analysis)
    assert analysis.derived_from == (t.id,)
    assert set(analysis.data) == {"topic", "entities"}
    assert analysis.metadata["backend"] == "gemini"
    assert analysis.metadata["usage"]["input_tokens"] == 500


async def test_extract_cache_hit(
    engine: Engine, fake_extract, mocker
) -> None:
    t = make_transcript(engine)
    kw = {"prompt": "p", "schema_def": _SCHEMA}
    [a1] = await engine.run("intelligence.extract", inputs=[t.id], **kw)
    spy = mocker.spy(fake_extract, "execute")
    [a2] = await engine.run("intelligence.extract", inputs=[t.id], **kw)
    assert spy.call_count == 0
    assert a1.id == a2.id


async def test_extract_param_change_new_id(
    engine: Engine, fake_extract
) -> None:
    t = make_transcript(engine)
    [a] = await engine.run(
        "intelligence.extract", inputs=[t.id], prompt="one",
        schema_def=_SCHEMA,
    )
    [b] = await engine.run(
        "intelligence.extract", inputs=[t.id], prompt="two",
        schema_def=_SCHEMA,
    )
    assert a.id != b.id


async def test_extract_rejects_non_text_kind(
    engine: Engine, sample_mp4: Path, fake_extract
) -> None:
    from media_engine.ops.acquire.upload import (
        AcquireUpload,
        AcquireUploadParams,
    )

    [video] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), ctx_for(engine)
    )
    engine.cache.upsert_artifact(video)
    with pytest.raises(ValueError, match="input kind mismatch"):
        await engine.run(
            "intelligence.extract", inputs=[video.id], prompt="x",
            schema_def=_SCHEMA,
        )


async def test_extract_bad_schema_fails_fast(
    engine: Engine, fake_extract
) -> None:
    t = make_transcript(engine)
    with pytest.raises(SchemaError, match="could not load schema"):
        await engine.run(
            "intelligence.extract", inputs=[t.id], prompt="x",
            schema_def="/no/such/schema.json",
        )


def test_build_extract_analysis_validates(
    engine: Engine, tmp_path: Path
) -> None:
    t = make_transcript(engine)
    params = ExtractParams(prompt="x", schema_def=_SCHEMA)
    wd = engine.storage.ensure_workdir("v")
    # Model returned JSON that violates the schema → SchemaError.
    with pytest.raises(SchemaError):
        build_extract_analysis(
            source=t, params=params, backend_name="gemini",
            backend_version="x", workdir_path=wd, storage=engine.storage,
            raw_text='{"topic": 5}', usage={},
        )


def test_cost_estimate_cloud_vs_local() -> None:
    op = IntelligenceExtract()
    cloud = op.cost_estimate([], ExtractParams(prompt="x", schema_def=_SCHEMA))
    assert cloud.cloud_cents > 0
    local = op.cost_estimate(
        [],
        ExtractParams(
            prompt="x", schema_def=_SCHEMA,
            model="mlx-community/Qwen2.5-7B-Instruct-4bit",
        ),
    )
    assert local.local_seconds > 0
    assert local.cloud_cents == 0


def _ctx(engine: Engine) -> OperationContext:
    return ctx_for(engine)


@pytest.mark.needs_gemini
@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai not installed")
async def test_real_gemini_smoke(engine: Engine) -> None:
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    t = make_transcript(
        engine, "Acme Corp announced a merger with Beta Inc in Berlin."
    )
    [a] = await engine.run(
        "intelligence.extract",
        inputs=[t.id],
        prompt="Extract the main topic and named entities.",
        schema_def=_SCHEMA,
        model="gemini-2.5-flash",
    )
    assert isinstance(a, Analysis)
    assert isinstance(a.data["topic"], str)
    assert isinstance(a.data["entities"], list)


@pytest.mark.skipif(
    not ANTHROPIC_AVAILABLE, reason="anthropic not installed"
)
async def test_real_claude_smoke(engine: Engine) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    t = make_transcript(
        engine, "Acme Corp announced a merger with Beta Inc in Berlin."
    )
    [a] = await engine.run(
        "intelligence.extract",
        inputs=[t.id],
        prompt="Extract the main topic and named entities.",
        schema_def=_SCHEMA,
        model="claude-haiku-4",
    )
    assert isinstance(a, Analysis)
    assert set(a.data) == {"topic", "entities"}


@pytest.mark.skipif(not MLX_LM_AVAILABLE, reason="mlx_lm not installed")
async def test_real_mlx_lm_smoke(engine: Engine) -> None:
    t = make_transcript(
        engine, "Acme Corp announced a merger with Beta Inc in Berlin."
    )
    [a] = await engine.run(
        "intelligence.extract",
        inputs=[t.id],
        prompt="Extract the main topic and named entities.",
        schema_def=_SCHEMA,
        model="mlx-community/Qwen2.5-7B-Instruct-4bit",
    )
    assert isinstance(a, Analysis)


async def test_provenance_records_model_dispatched_backend(
    engine: Engine,
) -> None:
    """Model-prefix dispatch (claude-*) must be the backend recorded in the
    cost ledger + run row — not the op's default_backend (gemini)."""
    from media_engine.backends import (
        Backend,
        BackendRegistry,
        BackendRequirements,
        register_backend,
    )

    BackendRegistry.unregister("intelligence.extract", "claude")

    @register_backend
    class _FakeClaude(Backend):
        op_name = "intelligence.extract"
        name = "claude"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def extract_invoke(self, source, params, ctx):
            return '{"topic":"t","entities":[]}', {
                "input_tokens": 10, "output_tokens": 2, "cost_cents": 0.01,
            }

        async def execute(self, inputs, params, ctx):
            raw, usage = await self.extract_invoke(inputs[0], params, ctx)
            return [
                build_extract_analysis(
                    source=inputs[0], params=params,
                    backend_name=self.name, backend_version=self.version,
                    workdir_path=ctx.workdir, storage=ctx.storage,
                    raw_text=raw, usage=usage,
                )
            ]

        def cost_estimate(self, inputs, params):
            from media_engine.ops import CostEstimate

            return CostEstimate(cloud_cents=0.01)

    try:
        t = make_transcript(engine, "claude-routed content")
        [a] = await engine.run(
            "intelligence.extract", inputs=[t.id], prompt="x",
            schema_def=_SCHEMA, model="claude-haiku-4",
        )
        assert a.metadata["backend"] == "claude"
        rows = engine.cost_log_entries()
        assert rows and rows[0].backend_name == "claude"
    finally:
        BackendRegistry.unregister("intelligence.extract", "claude")
        from media_engine.bootstrap import register_all

        register_all(force=True)
