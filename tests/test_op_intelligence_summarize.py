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


async def test_summarize_rejects_wrong_kind(
    engine: Engine, sample_mp4, fake_extract
) -> None:
    from media_engine.ops import OperationContext
    from media_engine.ops.acquire.upload import (
        AcquireUpload,
        AcquireUploadParams,
    )

    ctx = OperationContext(
        workdir=engine.storage.ensure_workdir("s"),
        config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace,
    )
    [video] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), ctx
    )
    engine.cache.upsert_artifact(video)
    with pytest.raises(ValueError, match="input kind mismatch"):
        await engine.run("intelligence.summarize", inputs=[video.id])


def test_cost_estimate_delegates() -> None:
    est = IntelligenceSummarize().cost_estimate([], SummarizeParams())
    assert est.cloud_cents > 0


async def test_summarize_forwards_extract_backend_param(
    engine: Engine, fake_extract, mocker
) -> None:
    """B-007: explicit `extract_backend` composite param routes the delegate
    `intelligence.extract` call to the chosen backend, overriding the
    default (which would be selected by model-prefix routing).
    """
    from media_engine.backends import (
        Backend,
        BackendRegistry,
        BackendRequirements,
        register_backend,
    )

    sentinel: dict[str, int] = {"calls": 0}
    BackendRegistry.unregister("intelligence.extract", "mlx-lm")

    @register_backend
    class _FakeExtractAlt(Backend):
        op_name = "intelligence.extract"
        name = "mlx-lm"
        version = "0.0.0-fake-alt"
        requires = BackendRequirements()

        async def execute(self, inputs, params, ctx):
            sentinel["calls"] += 1
            # Delegate to the existing fake so cache/persistence works.
            return await fake_extract().execute(inputs, params, ctx)

        def cost_estimate(self, inputs, params):
            from media_engine.ops import CostEstimate
            return CostEstimate(cloud_cents=0.05)

    try:
        t = make_transcript(engine)
        await engine.run(
            "intelligence.summarize",
            inputs=[t.id],
            extract_backend="mlx-lm",
        )
        assert sentinel["calls"] == 1
    finally:
        BackendRegistry.unregister("intelligence.extract", "mlx-lm")
        from media_engine.bootstrap import register_all
        register_all(force=True)


async def test_summarize_forwards_ctx_backend_when_param_unset(
    engine: Engine, fake_extract
) -> None:
    """B-007: when no explicit `extract_backend` is set, the engine-level
    `--backend` (ctx.backend on the composite) is forwarded to the delegate.
    """
    from media_engine.backends import (
        Backend,
        BackendRegistry,
        BackendRequirements,
        register_backend,
    )

    sentinel: dict[str, int] = {"calls": 0}
    BackendRegistry.unregister("intelligence.extract", "mlx-lm")

    @register_backend
    class _FakeExtractAlt(Backend):
        op_name = "intelligence.extract"
        name = "mlx-lm"
        version = "0.0.0-fake-alt"
        requires = BackendRequirements()

        async def execute(self, inputs, params, ctx):
            sentinel["calls"] += 1
            return await fake_extract().execute(inputs, params, ctx)

        def cost_estimate(self, inputs, params):
            from media_engine.ops import CostEstimate
            return CostEstimate(cloud_cents=0.05)

    try:
        t = make_transcript(engine)
        # Pass --backend on the composite itself — the engine preserves
        # it in ctx.backend (per the _resolve_backend tweak), and the
        # composite's run() reads ctx.backend when no explicit
        # extract_backend param is set.
        await engine.run(
            "intelligence.summarize", inputs=[t.id], backend="mlx-lm"
        )
        assert sentinel["calls"] == 1
    finally:
        BackendRegistry.unregister("intelligence.extract", "mlx-lm")
        from media_engine.bootstrap import register_all
        register_all(force=True)


async def test_summarize_explicit_param_beats_ctx_backend(
    engine: Engine, fake_extract
) -> None:
    """B-007 precedence: explicit param > ctx.backend > delegate default."""
    from media_engine.backends import (
        Backend,
        BackendRegistry,
        BackendRequirements,
        register_backend,
    )

    explicit_calls = {"n": 0}
    ctx_calls = {"n": 0}
    BackendRegistry.unregister("intelligence.extract", "claude")
    BackendRegistry.unregister("intelligence.extract", "mlx-lm")

    @register_backend
    class _FakeExplicit(Backend):
        op_name = "intelligence.extract"
        name = "claude"
        version = "0.0.0-fake-explicit"
        requires = BackendRequirements()

        async def execute(self, inputs, params, ctx):
            explicit_calls["n"] += 1
            return await fake_extract().execute(inputs, params, ctx)

        def cost_estimate(self, inputs, params):
            from media_engine.ops import CostEstimate
            return CostEstimate(cloud_cents=0.05)

    @register_backend
    class _FakeCtx(Backend):
        op_name = "intelligence.extract"
        name = "mlx-lm"
        version = "0.0.0-fake-ctx"
        requires = BackendRequirements()

        async def execute(self, inputs, params, ctx):
            ctx_calls["n"] += 1
            return await fake_extract().execute(inputs, params, ctx)

        def cost_estimate(self, inputs, params):
            from media_engine.ops import CostEstimate
            return CostEstimate(cloud_cents=0.05)

    try:
        t = make_transcript(engine)
        await engine.run(
            "intelligence.summarize",
            inputs=[t.id],
            backend="mlx-lm",  # ctx.backend
            extract_backend="claude",  # explicit — wins
        )
        assert explicit_calls["n"] == 1
        assert ctx_calls["n"] == 0
    finally:
        BackendRegistry.unregister("intelligence.extract", "claude")
        BackendRegistry.unregister("intelligence.extract", "mlx-lm")
        from media_engine.bootstrap import register_all
        register_all(force=True)


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
