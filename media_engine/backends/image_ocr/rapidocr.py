"""``rapidocr`` backend for ``image.ocr`` (local ONNX, no API key).

Lazy import; the engine works without ``rapidocr-onnxruntime`` and only
fails at execute() with an install hint. RapidOCR returns
``[[box, text, score], ...]``; we normalize to the engine's region shape.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Image
from media_engine.backends import Backend, BackendRequirements, register_backend
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.image.ocr import ImageOCRParams, build_ocr_artifact

BACKEND_NAME = "rapidocr"
BACKEND_VERSION = "1.0.0"


def _run_rapidocr(image_path: str) -> list[dict[str, Any]]:
    try:
        mod: Any = importlib.import_module("rapidocr_onnxruntime")
    except ImportError as e:
        raise RuntimeError(
            "rapidocr-onnxruntime is not installed. Install with: "
            "uv sync --extra ocr"
        ) from e
    out: Any = mod.RapidOCR()(image_path)
    # RapidOCR returns ``(result, elapsed)``; ``result`` is
    # ``[[box, text, score], ...]`` or ``None`` when nothing is found.
    items: list[Any] = list(out[0]) if out and out[0] else []
    regions: list[dict[str, Any]] = []
    for entry in items:
        box: Any = entry[0]
        text: Any = entry[1]
        score: Any = entry[2]
        xs: list[float] = [float(p[0]) for p in box]
        ys: list[float] = [float(p[1]) for p in box]
        regions.append({
            "text": str(text),
            "bbox": [min(xs), min(ys), max(xs), max(ys)],
            "confidence": float(score),
        })
    return regions


@register_backend
class RapidOCRBackend(Backend):
    op_name = "image.ocr"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(services=["rapidocr-onnxruntime"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ImageOCRParams)
        image = inputs[0]
        assert isinstance(image, Image)
        regions = await asyncio.to_thread(_run_rapidocr, str(image.path))
        full_text = "\n".join(r["text"] for r in regions)
        return [
            build_ocr_artifact(
                image=image,
                params=params,
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                workdir_path=ctx.workdir,
                storage=ctx.storage,
                regions=regions,
                full_text=full_text,
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=1.0)


__all__ = ["RapidOCRBackend"]
