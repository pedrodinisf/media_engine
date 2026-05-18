"""``gemini`` backend for ``image.classify``.

Asks the model to pick the single best label and rate each — useful when
the labels need reasoning open-clip can't do. Parses a strict JSON reply;
falls back to a uniform distribution if the model doesn't comply.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Image
from media_engine.backends import Backend, BackendRequirements, register_backend
from media_engine.backends._gemini_vision import gemini_vision_sync, require_api_key
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.image.classify import (
    ImageClassifyParams,
    build_classify_artifact,
)

BACKEND_NAME = "gemini"
BACKEND_VERSION = "1.0.0"


def _parse_scores(text: str, labels: list[str]) -> dict[str, float]:
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        obj: Any = json.loads(text[start:end])
        scores = {
            label: float(obj.get(label, 0.0))
            for label in labels
        }
        total = sum(scores.values()) or 1.0
        return {k: v / total for k, v in scores.items()}
    except (ValueError, json.JSONDecodeError, TypeError):
        uniform = 1.0 / len(labels)
        return dict.fromkeys(labels, uniform)


@register_backend
class GeminiClassifyBackend(Backend):
    op_name = "image.classify"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(env=["GEMINI_API_KEY"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ImageClassifyParams)
        image = inputs[0]
        assert isinstance(image, Image)
        api_key = require_api_key()
        labels_csv = ", ".join(params.labels)
        prompt = (
            f"Classify this image against these labels: {labels_csv}. "
            f"Reply with ONLY a JSON object mapping each label to a "
            f"probability 0..1 that sums to 1. No prose."
        )
        text, _usage = await asyncio.to_thread(
            gemini_vision_sync,
            api_key=api_key,
            model=params.model,
            image_bytes=[image.path.read_bytes()],
            prompt=prompt,
            temperature=0.0,
            max_tokens=256,
        )
        scores = _parse_scores(text, params.labels)
        return [
            build_classify_artifact(
                image=image,
                params=params,
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                workdir_path=ctx.workdir,
                storage=ctx.storage,
                scores=scores,
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        from media_engine.backends._pricing import estimate_cost_cents

        assert isinstance(params, ImageClassifyParams)
        return CostEstimate(
            cloud_cents=estimate_cost_cents(params.model, 258, 128),
            tokens_in=258,
            tokens_out=128,
        )


__all__ = ["GeminiClassifyBackend", "_parse_scores"]
