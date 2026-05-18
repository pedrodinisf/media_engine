"""``open-clip`` backend for ``image.classify`` (local zero-shot).

Lazy import of open_clip_torch + PIL; cached model via ctx.model_pool.
Computes softmax over image↔label cosine similarity.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Image
from media_engine.backends import Backend, BackendRequirements, register_backend
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.image.classify import (
    ImageClassifyParams,
    build_classify_artifact,
)

BACKEND_NAME = "open-clip"
BACKEND_VERSION = "1.0.0"


def _load_clip(arch: str) -> Any:
    try:
        open_clip: Any = importlib.import_module("open_clip")
        torch: Any = importlib.import_module("torch")
    except ImportError as e:
        raise RuntimeError(
            "open_clip_torch is not installed. Install with: "
            "uv sync --extra classify"
        ) from e
    model, _, preprocess = open_clip.create_model_and_transforms(
        arch, pretrained="laion2b_s34b_b79k"
    )
    model.eval()
    tokenizer = open_clip.get_tokenizer(arch)
    return {"model": model, "preprocess": preprocess,
            "tokenizer": tokenizer, "torch": torch}


def _classify_sync(
    bundle: Any, image_path: str, labels: list[str]
) -> dict[str, float]:
    pil_image: Any = importlib.import_module("PIL.Image")

    torch: Any = bundle["torch"]
    model: Any = bundle["model"]
    preprocess: Any = bundle["preprocess"]
    tokenizer: Any = bundle["tokenizer"]

    pil_img: Any = pil_image.open(image_path).convert("RGB")
    img: Any = preprocess(pil_img).unsqueeze(0)
    text = tokenizer([f"a photo of a {label}" for label in labels])
    with torch.no_grad():
        img_features = model.encode_image(img)
        txt_features = model.encode_text(text)
        img_features /= img_features.norm(dim=-1, keepdim=True)
        txt_features /= txt_features.norm(dim=-1, keepdim=True)
        probs = (100.0 * img_features @ txt_features.T).softmax(dim=-1)[0]
    return {label: float(probs[i]) for i, label in enumerate(labels)}


@register_backend
class OpenClipClassifyBackend(Backend):
    op_name = "image.classify"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(services=["open_clip_torch"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ImageClassifyParams)
        image = inputs[0]
        assert isinstance(image, Image)

        cache_key = f"open-clip:{params.model}"
        if ctx.model_pool is not None:
            bundle = await asyncio.to_thread(
                ctx.model_pool.get_or_load,
                cache_key,
                lambda: _load_clip(params.model),
            )
        else:
            bundle = await asyncio.to_thread(_load_clip, params.model)

        scores = await asyncio.to_thread(
            _classify_sync, bundle, str(image.path), params.labels
        )
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
        return CostEstimate(local_seconds=0.5)


__all__ = ["OpenClipClassifyBackend"]
