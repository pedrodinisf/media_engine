"""Tests for ops/image/describe.py + the Gemini backend.

Dispatch tests use a fake backend (always run). Real Gemini smoke is
`needs_gemini`-gated.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import BaseModel

from media_engine.artifacts import Analysis, AnyArtifact, Image, Kind
from media_engine.backends import (
    Backend,
    BackendRegistry,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.ops.image.describe import (
    ImageDescribe,
    ImageDescribeParams,
    build_image_analysis,
)
from media_engine.runtime.engine import Engine


def _genai_available() -> bool:
    try:
        return importlib.util.find_spec("google.genai") is not None
    except ModuleNotFoundError:
        return False


GENAI_AVAILABLE = _genai_available()


def _ctx_for(engine: Engine) -> OperationContext:
    return OperationContext(
        workdir=engine.storage.ensure_workdir("id-test"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
    )


def test_op_class_attributes() -> None:
    assert ImageDescribe.name == "image.describe"
    assert ImageDescribe.input_kinds == (Kind.Image,)
    assert ImageDescribe.output_kinds == (Kind.Analysis,)
    assert ImageDescribe.default_backend == "gemini"


def test_params_defaults() -> None:
    p = ImageDescribeParams()
    assert p.model == "gemini-2.5-flash"
    assert "Describe" in p.prompt


@pytest.fixture
def fake_id_backend() -> type[Backend]:
    BackendRegistry.unregister("image.describe", "gemini")

    @register_backend
    class _Fake(Backend):
        op_name = "image.describe"
        name = "gemini"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self,
            inputs: list[AnyArtifact],
            params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, ImageDescribeParams)
            img = inputs[0]
            assert isinstance(img, Image)
            return [
                build_image_analysis(
                    image=img,
                    params=params,
                    backend_name=self.name,
                    backend_version=self.version,
                    workdir_path=ctx.workdir,
                    storage=ctx.storage,
                    text=f"An image. Prompt: {params.prompt}",
                    usage={"input_tokens": 260, "output_tokens": 30,
                           "cost_cents": 0.05},
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate(cloud_cents=0.1)

    yield _Fake
    BackendRegistry.unregister("image.describe", "gemini")
    from media_engine.bootstrap import register_all
    register_all(force=True)


async def _image(engine: Engine, sample_png: Path) -> Image:
    [img] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_png), _ctx_for(engine)
    )
    assert isinstance(img, Image)
    engine.cache.upsert_artifact(img)
    return img


async def test_describe_via_fake_backend(
    engine: Engine, sample_png: Path, fake_id_backend
) -> None:
    img = await _image(engine, sample_png)
    [analysis] = await engine.run(
        "image.describe", inputs=[img.id], prompt="What is this?"
    )
    assert isinstance(analysis, Analysis)
    assert analysis.derived_from == (img.id,)
    assert "What is this?" in analysis.data["text"]
    assert analysis.metadata["usage"]["input_tokens"] == 260


async def test_describe_cache_hit(
    engine: Engine, sample_png: Path, fake_id_backend, mocker
) -> None:
    img = await _image(engine, sample_png)
    [a1] = await engine.run("image.describe", inputs=[img.id], prompt="p")
    spy = mocker.spy(fake_id_backend, "execute")
    [a2] = await engine.run("image.describe", inputs=[img.id], prompt="p")
    assert spy.call_count == 0
    assert a1.id == a2.id


async def test_describe_param_change_new_id(
    engine: Engine, sample_png: Path, fake_id_backend
) -> None:
    img = await _image(engine, sample_png)
    [a] = await engine.run("image.describe", inputs=[img.id], prompt="one")
    [b] = await engine.run("image.describe", inputs=[img.id], prompt="two")
    assert a.id != b.id


async def test_describe_rejects_non_image(
    engine: Engine, sample_mp4: Path, fake_id_backend
) -> None:
    [video] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), _ctx_for(engine)
    )
    engine.cache.upsert_artifact(video)
    with pytest.raises(ValueError, match="kind mismatch"):
        await engine.run("image.describe", inputs=[video.id], prompt="x")


def test_cost_estimate_uses_pricing() -> None:
    op = ImageDescribe()
    est = op.cost_estimate([], ImageDescribeParams())
    assert est.tokens_in == 258
    assert est.cloud_cents > 0


@pytest.mark.needs_gemini
@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai not installed")
async def test_real_gemini_smoke(engine: Engine, sample_png: Path) -> None:
    import os

    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    img = await _image(engine, sample_png)
    [analysis] = await engine.run(
        "image.describe",
        inputs=[img.id],
        prompt="Describe this image in one sentence.",
    )
    assert isinstance(analysis, Analysis)
    assert len(analysis.data["text"]) > 0
