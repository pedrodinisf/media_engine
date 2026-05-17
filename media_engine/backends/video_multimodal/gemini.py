"""``gemini`` backend for ``video.multimodal`` (ported from framepulse cloud).

Flow: upload the file via the Gemini File API → poll until ACTIVE →
``generate_content_stream`` (Progress per chunk) → collect
``usage_metadata`` → **always** delete the remote file in ``finally``
(Google auto-expires after 48 h but we don't rely on that).

Sync google-genai SDK wrapped in ``asyncio.to_thread`` so the daemon's
loop stays responsive. Optional dep: ``uv sync --extra vlm-cloud``.
Requires ``GEMINI_API_KEY``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Video
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.backends._pricing import (
    RESOLUTION_API_VALUE,
    estimate_cost_cents,
    estimate_video_tokens,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.video.multimodal import (
    MultimodalVideoParams,
    build_multimodal_analysis_artifact,
)
from media_engine.runtime.events import Progress

BACKEND_NAME = "gemini"
BACKEND_VERSION = "1.0.0"

_UPLOAD_POLL_TIMEOUT_S = 600
_UPLOAD_POLL_INTERVAL_S = 3


def _import_genai() -> tuple[Any, Any]:
    import importlib

    try:
        genai_mod: Any = importlib.import_module("google.genai")
        types_mod: Any = importlib.import_module("google.genai.types")
    except ImportError as e:
        raise RuntimeError(
            "google-genai is not installed. Install with: "
            "uv sync --extra vlm-cloud"
        ) from e
    return genai_mod, types_mod


def _emit(ctx: OperationContext, run_id: str, frac: float, msg: str) -> None:
    with contextlib.suppress(Exception):
        ctx.emit(
            Progress(
                event_id=uuid4().hex,
                op_run_id=run_id,
                timestamp=datetime.now(UTC),
                fraction=max(0.0, min(1.0, frac)),
                message=msg,
                phase="gemini",
            )
        )


def _state_name(file_obj: Any) -> str:
    st = file_obj.state
    if isinstance(st, str):
        return st
    return getattr(st, "name", str(st))


def _run_gemini_sync(
    api_key: str,
    video_path: str,
    params: MultimodalVideoParams,
) -> tuple[str, dict[str, Any]]:
    """Blocking Gemini call. Returns (response_text, usage dict)."""
    genai, types = _import_genai()
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=600_000),
    )
    gemini_file = client.files.upload(file=video_path)
    try:
        start = time.time()
        f = client.files.get(name=gemini_file.name)
        while _state_name(f) == "PROCESSING":
            if time.time() - start > _UPLOAD_POLL_TIMEOUT_S:
                raise RuntimeError(
                    f"Gemini video processing timed out after "
                    f"{_UPLOAD_POLL_TIMEOUT_S // 60} min. Try a shorter clip."
                )
            time.sleep(_UPLOAD_POLL_INTERVAL_S)
            f = client.files.get(name=gemini_file.name)
        if _state_name(f) == "FAILED":
            raise RuntimeError("Gemini video processing FAILED server-side.")

        effective_prompt = params.prompt
        if params.additional_instructions:
            effective_prompt += f"\n\n{params.additional_instructions}"

        gen_config = types.GenerateContentConfig(
            system_instruction=params.system_prompt,
            temperature=params.temperature,
            max_output_tokens=params.max_tokens,
        )
        api_res = RESOLUTION_API_VALUE.get(params.media_resolution.lower())
        if api_res is not None:
            gen_config.media_resolution = api_res

        text = ""
        usage_meta: Any = None
        for chunk in client.models.generate_content_stream(
            model=params.model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_uri(
                            file_uri=f.uri, mime_type=f.mime_type
                        ),
                        types.Part.from_text(text=effective_prompt),
                    ],
                )
            ],
            config=gen_config,
        ):
            if getattr(chunk, "text", None):
                text += chunk.text
            if getattr(chunk, "usage_metadata", None):
                usage_meta = chunk.usage_metadata

        usage: dict[str, Any] = {}
        if usage_meta is not None:
            usage = {
                "input_tokens": getattr(usage_meta, "prompt_token_count", 0) or 0,
                "output_tokens": getattr(
                    usage_meta, "candidates_token_count", 0
                ) or 0,
                "thinking_tokens": getattr(
                    usage_meta, "thoughts_token_count", 0
                ) or 0,
                "total_tokens": getattr(usage_meta, "total_token_count", 0) or 0,
            }
            usage["cost_cents"] = estimate_cost_cents(
                params.model,
                usage["input_tokens"],
                usage["output_tokens"],
            )
        return text, usage
    finally:
        with contextlib.suppress(Exception):
            client.files.delete(name=gemini_file.name)


@register_backend
class GeminiVideoMultimodalBackend(Backend):
    op_name = "video.multimodal"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(env=["GEMINI_API_KEY"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, MultimodalVideoParams)
        video = inputs[0]
        assert isinstance(video, Video)

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY env var not set. Get a key at "
                "https://aistudio.google.com/apikey and `export GEMINI_API_KEY=...`."
            )

        run_id = uuid4().hex
        _emit(ctx, run_id, 0.05, "uploading to Gemini")
        text, usage = await asyncio.to_thread(
            _run_gemini_sync, api_key, str(video.path), params
        )
        _emit(ctx, run_id, 1.0, f"done ({len(text)} chars)")

        return [
            build_multimodal_analysis_artifact(
                video=video,
                params=params,
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                workdir_path=ctx.workdir,
                storage=ctx.storage,
                text=text,
                usage=usage,
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, MultimodalVideoParams)
        if not inputs:
            return CostEstimate()
        video = inputs[0]
        if not isinstance(video, Video) or video.duration is None:
            return CostEstimate(cloud_cents=1.0)
        tokens_in = estimate_video_tokens(video.duration, params.media_resolution)
        # Assume output ≈ max_tokens for an upper-bound preview.
        cents = estimate_cost_cents(params.model, tokens_in, params.max_tokens)
        return CostEstimate(
            cloud_cents=cents,
            tokens_in=tokens_in,
            tokens_out=params.max_tokens,
        )


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "GeminiVideoMultimodalBackend"]
