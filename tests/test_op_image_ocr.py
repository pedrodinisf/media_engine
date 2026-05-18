"""Tests for ops/image/ocr.py + the rapidocr / gemini-vision backends.

Dispatch tests use a fake backend (always run). The real rapidocr smoke
needs the optional ``ocr`` extra + PIL to render a text fixture; the
gemini-vision smoke is `needs_gemini`-gated.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Image, Kind, OCRText
from media_engine.backends import (
    Backend,
    BackendRegistry,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.ops.image.ocr import (
    ImageOCR,
    ImageOCRParams,
    build_ocr_artifact,
)
from media_engine.runtime.engine import Engine


def _genai_available() -> bool:
    try:
        return importlib.util.find_spec("google.genai") is not None
    except ModuleNotFoundError:
        return False


GENAI_AVAILABLE = _genai_available()
RAPIDOCR_AVAILABLE = (
    importlib.util.find_spec("rapidocr_onnxruntime") is not None
)


def _ctx_for(engine: Engine) -> OperationContext:
    return OperationContext(
        workdir=engine.storage.ensure_workdir("ocr-test"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
    )


def test_op_class_attributes() -> None:
    assert ImageOCR.name == "image.ocr"
    assert ImageOCR.input_kinds == (Kind.Image,)
    assert ImageOCR.output_kinds == (Kind.OCRText,)
    assert ImageOCR.default_backend == "rapidocr"


def test_params_defaults() -> None:
    p = ImageOCRParams()
    assert p.backend == "rapidocr"


@pytest.fixture
def fake_ocr_backend() -> type[Backend]:
    BackendRegistry.unregister("image.ocr", "rapidocr")

    @register_backend
    class _Fake(Backend):
        op_name = "image.ocr"
        name = "rapidocr"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self,
            inputs: list[AnyArtifact],
            params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, ImageOCRParams)
            img = inputs[0]
            assert isinstance(img, Image)
            regions = [
                {"text": "MEDIA", "bbox": [0.0, 0.0, 10.0, 5.0],
                 "confidence": 0.99},
                {"text": "ENGINE", "bbox": [0.0, 6.0, 12.0, 11.0],
                 "confidence": 0.98},
            ]
            return [
                build_ocr_artifact(
                    image=img,
                    params=params,
                    backend_name=self.name,
                    backend_version=self.version,
                    workdir_path=ctx.workdir,
                    storage=ctx.storage,
                    regions=regions,
                    full_text="MEDIA\nENGINE",
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate(local_seconds=1.0)

    yield _Fake
    BackendRegistry.unregister("image.ocr", "rapidocr")
    from media_engine.bootstrap import register_all
    register_all(force=True)


async def _image(engine: Engine, sample_png: Path) -> Image:
    [img] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_png), _ctx_for(engine)
    )
    assert isinstance(img, Image)
    engine.cache.upsert_artifact(img)
    return img


async def test_ocr_via_fake_backend(
    engine: Engine, sample_png: Path, fake_ocr_backend
) -> None:
    img = await _image(engine, sample_png)
    [ocr] = await engine.run("image.ocr", inputs=[img.id])
    assert isinstance(ocr, OCRText)
    assert ocr.derived_from == (img.id,)
    assert ocr.metadata["text"] == "MEDIA\nENGINE"
    assert len(ocr.regions) == 2
    assert ocr.regions[0]["text"] == "MEDIA"


async def test_ocr_cache_hit(
    engine: Engine, sample_png: Path, fake_ocr_backend, mocker
) -> None:
    img = await _image(engine, sample_png)
    [o1] = await engine.run("image.ocr", inputs=[img.id])
    spy = mocker.spy(fake_ocr_backend, "execute")
    [o2] = await engine.run("image.ocr", inputs=[img.id])
    assert spy.call_count == 0
    assert o1.id == o2.id


async def test_ocr_rejects_non_image(
    engine: Engine, sample_mp4: Path, fake_ocr_backend
) -> None:
    [video] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), _ctx_for(engine)
    )
    engine.cache.upsert_artifact(video)
    with pytest.raises(ValueError, match="kind mismatch"):
        await engine.run("image.ocr", inputs=[video.id])


def test_cost_estimate_local_vs_cloud() -> None:
    op = ImageOCR()
    local = op.cost_estimate([], ImageOCRParams())
    assert local.local_seconds > 0
    assert local.cloud_cents == 0
    cloud = op.cost_estimate([], ImageOCRParams(backend="gemini-vision"))
    assert cloud.cloud_cents > 0


@pytest.mark.skipif(
    not RAPIDOCR_AVAILABLE, reason="rapidocr-onnxruntime not installed"
)
async def test_real_rapidocr_smoke(engine: Engine, tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL")
    from PIL import Image as PILImage
    from PIL import ImageDraw

    assert pil
    canvas = PILImage.new("RGB", (320, 96), "white")
    ImageDraw.Draw(canvas).text((10, 30), "MEDIA ENGINE", fill="black")
    src = tmp_path / "ocr_fixture.png"
    canvas.save(src)

    [img] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=src), _ctx_for(engine)
    )
    engine.cache.upsert_artifact(img)
    # Default-backend path goes through the engine (cache + dispatch).
    [ocr] = await engine.run("image.ocr", inputs=[img.id])
    assert isinstance(ocr, OCRText)
    assert any(r.get("confidence") for r in ocr.regions)


@pytest.mark.needs_gemini
@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai not installed")
async def test_real_gemini_vision_smoke(
    engine: Engine, sample_png: Path
) -> None:
    import os

    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    img = await _image(engine, sample_png)
    # `backend` is an op param (collides with Engine.run's reserved
    # `backend=` kwarg), so the non-default backend is selected by
    # invoking the op directly with explicit params.
    [ocr] = await ImageOCR().run(
        [img], ImageOCRParams(backend="gemini-vision"), _ctx_for(engine)
    )
    assert isinstance(ocr, OCRText)
    assert isinstance(ocr.metadata["text"], str)
