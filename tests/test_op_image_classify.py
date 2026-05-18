"""Tests for ops/image/classify.py + the open-clip / gemini backends.

Dispatch tests use a fake backend (always run). The real open-clip smoke
needs the optional ``classify`` extra; the gemini smoke is
`needs_gemini`-gated.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from media_engine.artifacts import Analysis, AnyArtifact, Image, Kind
from media_engine.backends import (
    Backend,
    BackendRegistry,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.ops.image.classify import (
    ImageClassify,
    ImageClassifyParams,
    build_classify_artifact,
)
from media_engine.runtime.engine import Engine


def _genai_available() -> bool:
    try:
        return importlib.util.find_spec("google.genai") is not None
    except ModuleNotFoundError:
        return False


GENAI_AVAILABLE = _genai_available()
OPEN_CLIP_AVAILABLE = importlib.util.find_spec("open_clip") is not None


def _ctx_for(engine: Engine) -> OperationContext:
    return OperationContext(
        workdir=engine.storage.ensure_workdir("ic-test"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
    )


def test_op_class_attributes() -> None:
    assert ImageClassify.name == "image.classify"
    assert ImageClassify.input_kinds == (Kind.Image,)
    assert ImageClassify.output_kinds == (Kind.Analysis,)
    assert ImageClassify.default_backend == "open-clip"


def test_params_require_labels() -> None:
    with pytest.raises(ValidationError):
        ImageClassifyParams(labels=[])
    p = ImageClassifyParams(labels=["cat", "dog"])
    assert p.backend == "open-clip"


@pytest.fixture
def fake_ic_backend() -> type[Backend]:
    BackendRegistry.unregister("image.classify", "open-clip")

    @register_backend
    class _Fake(Backend):
        op_name = "image.classify"
        name = "open-clip"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self,
            inputs: list[AnyArtifact],
            params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, ImageClassifyParams)
            img = inputs[0]
            assert isinstance(img, Image)
            n = len(params.labels)
            # First label wins by construction.
            scores = {lbl: (0.6 if i == 0 else 0.4 / (n - 1 or 1))
                      for i, lbl in enumerate(params.labels)}
            return [
                build_classify_artifact(
                    image=img,
                    params=params,
                    backend_name=self.name,
                    backend_version=self.version,
                    workdir_path=ctx.workdir,
                    storage=ctx.storage,
                    scores=scores,
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate(local_seconds=0.5)

    yield _Fake
    BackendRegistry.unregister("image.classify", "open-clip")
    from media_engine.bootstrap import register_all
    register_all(force=True)


async def _image(engine: Engine, sample_png: Path) -> Image:
    [img] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_png), _ctx_for(engine)
    )
    assert isinstance(img, Image)
    engine.cache.upsert_artifact(img)
    return img


async def test_classify_via_fake_backend(
    engine: Engine, sample_png: Path, fake_ic_backend
) -> None:
    img = await _image(engine, sample_png)
    [analysis] = await engine.run(
        "image.classify", inputs=[img.id], labels=["screenshot", "landscape"]
    )
    assert isinstance(analysis, Analysis)
    assert analysis.derived_from == (img.id,)
    assert analysis.data["top"] == "screenshot"
    assert analysis.data["labels"] == ["screenshot", "landscape"]
    assert pytest.approx(sum(analysis.data["scores"].values()), abs=1e-6) == 1.0


async def test_classify_cache_hit(
    engine: Engine, sample_png: Path, fake_ic_backend, mocker
) -> None:
    img = await _image(engine, sample_png)
    [a1] = await engine.run(
        "image.classify", inputs=[img.id], labels=["a", "b"]
    )
    spy = mocker.spy(fake_ic_backend, "execute")
    [a2] = await engine.run(
        "image.classify", inputs=[img.id], labels=["a", "b"]
    )
    assert spy.call_count == 0
    assert a1.id == a2.id


async def test_classify_param_change_new_id(
    engine: Engine, sample_png: Path, fake_ic_backend
) -> None:
    img = await _image(engine, sample_png)
    [a] = await engine.run(
        "image.classify", inputs=[img.id], labels=["a", "b"]
    )
    [b] = await engine.run(
        "image.classify", inputs=[img.id], labels=["b", "a"]
    )
    assert a.id != b.id


async def test_classify_rejects_non_image(
    engine: Engine, sample_mp4: Path, fake_ic_backend
) -> None:
    [video] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), _ctx_for(engine)
    )
    engine.cache.upsert_artifact(video)
    with pytest.raises(ValueError, match="kind mismatch"):
        await engine.run(
            "image.classify", inputs=[video.id], labels=["a", "b"]
        )


def test_cost_estimate_local_vs_cloud() -> None:
    op = ImageClassify()
    local = op.cost_estimate([], ImageClassifyParams(labels=["a"]))
    assert local.local_seconds > 0
    assert local.cloud_cents == 0
    cloud = op.cost_estimate(
        [], ImageClassifyParams(labels=["a"], backend="gemini")
    )
    assert cloud.cloud_cents > 0


@pytest.mark.skipif(
    not OPEN_CLIP_AVAILABLE, reason="open_clip_torch not installed"
)
async def test_real_open_clip_smoke(
    engine: Engine, sample_png: Path
) -> None:
    img = await _image(engine, sample_png)
    labels = ["a screenshot of text", "a photo of a dog", "a landscape"]
    [analysis] = await engine.run(
        "image.classify", inputs=[img.id], labels=labels
    )
    assert isinstance(analysis, Analysis)
    assert analysis.data["top"] in labels
    assert pytest.approx(sum(analysis.data["scores"].values()), abs=1e-3) == 1.0


@pytest.mark.needs_gemini
@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai not installed")
async def test_real_gemini_smoke(engine: Engine, sample_png: Path) -> None:
    import os

    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    img = await _image(engine, sample_png)
    # `backend` collides with Engine.run's reserved kwarg → invoke directly.
    [analysis] = await ImageClassify().run(
        [img],
        ImageClassifyParams(
            labels=["a screenshot of text", "a photo of a dog"],
            backend="gemini",
        ),
        _ctx_for(engine),
    )
    assert isinstance(analysis, Analysis)
    assert analysis.data["top"] in analysis.data["labels"]
