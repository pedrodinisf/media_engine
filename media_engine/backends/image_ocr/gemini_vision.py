"""``gemini-vision`` backend for ``image.ocr`` (fallback, cloud).

Better than RapidOCR on stylized/handwritten text. The model returns
plain transcribed text; we record it as a single full-image region
(no per-word boxes — that's RapidOCR's strength).
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Image
from media_engine.backends import Backend, BackendRequirements, register_backend
from media_engine.backends._gemini_vision import gemini_vision_sync, require_api_key
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.image.ocr import ImageOCRParams, build_ocr_artifact

BACKEND_NAME = "gemini-vision"
BACKEND_VERSION = "1.0.0"

_OCR_PROMPT = (
    "Transcribe ALL text visible in this image exactly, preserving line "
    "breaks. Output only the transcribed text, nothing else."
)


@register_backend
class GeminiVisionOCRBackend(Backend):
    op_name = "image.ocr"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(env=["GEMINI_API_KEY"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ImageOCRParams)
        image = inputs[0]
        assert isinstance(image, Image)
        api_key = require_api_key()
        text, _usage = await asyncio.to_thread(
            gemini_vision_sync,
            api_key=api_key,
            model=params.model,
            image_bytes=[image.path.read_bytes()],
            prompt=_OCR_PROMPT,
            temperature=0.0,
            max_tokens=4096,
        )
        text = text.strip()
        regions = (
            [{"text": text, "bbox": None, "confidence": None}] if text else []
        )
        return [
            build_ocr_artifact(
                image=image,
                params=params,
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                workdir_path=ctx.workdir,
                storage=ctx.storage,
                regions=regions,
                full_text=text,
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        from media_engine.backends._pricing import estimate_cost_cents

        assert isinstance(params, ImageOCRParams)
        return CostEstimate(
            cloud_cents=estimate_cost_cents(params.model, 258, 512),
            tokens_in=258,
            tokens_out=512,
        )


__all__ = ["GeminiVisionOCRBackend"]
