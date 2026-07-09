"""``image.ocr`` — Image → OCRText.

Default backend: ``rapidocr`` (ONNX, local, no API key). ``gemini-vision``
is the fallback when the user explicitly picks it (``--backend
gemini-vision``) — better on stylized/handwritten text, costs cents.

OCRText.metadata.regions = [{text, bbox:[x0,y0,x1,y1], confidence}].
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from media_engine.artifacts import (
    AnyArtifact,
    Image,
    Kind,
    OCRText,
    compute_derived_artifact_id,
)
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.ops.video._models import VLM_GEMINI_MODELS


class ImageOCRParams(BaseModel):
    # Backend is selected the engine-standard way (``--backend
    # gemini-vision`` / ``backend=`` / DAGNode.backend), not a param —
    # default is ``rapidocr`` (see Operation.default_backend).
    # only used by the gemini-vision backend
    model: Annotated[
        str,
        Field(json_schema_extra={"enum": list(VLM_GEMINI_MODELS)}),
    ] = "gemini-2.5-flash"


@register_op
class ImageOCR(Operation):
    """Extract text (with bounding boxes) from an image."""

    name = "image.ocr"
    version = "1.0.0"
    input_kinds = (Kind.Image,)
    output_kinds = (Kind.OCRText,)
    params_model = ImageOCRParams
    default_backend = "rapidocr"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ImageOCRParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Image):
            raise ValueError(
                f"image.ocr expects exactly one Image input, "
                f"got {[a.kind for a in inputs]}"
            )
        backend_name = ctx.backend or self.default_backend
        assert backend_name is not None
        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute([inputs[0]], params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, ImageOCRParams)
        # Default (rapidocr) is local; the gemini-vision backend reports its
        # own cloud usage post-run. cost_estimate has no ctx, so the pre-run
        # estimate reflects the default local path.
        return CostEstimate(local_seconds=1.0)


def build_ocr_artifact(
    *,
    image: Image,
    params: ImageOCRParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    regions: list[dict[str, Any]],
    full_text: str,
) -> OCRText:
    derived_id = compute_derived_artifact_id(
        kind=Kind.OCRText,
        op_name="image.ocr",
        op_version="1.0.0",
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[image.id],
    )
    payload: dict[str, Any] = {
        "regions": regions,
        "text": full_text,
        "backend": backend_name,
    }
    tmp = workdir_path / f"ocr-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dest = storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)
    return OCRText(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=(image.id,),
        created_at=datetime.now(UTC),
    )
