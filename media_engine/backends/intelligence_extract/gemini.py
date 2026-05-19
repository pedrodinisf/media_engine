"""``gemini`` backend for ``intelligence.extract`` (text-only)."""

from __future__ import annotations

import asyncio
import importlib
import os
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact
from media_engine.backends import Backend, BackendRequirements, register_backend
from media_engine.backends._pricing import estimate_cost_cents
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.intelligence.extract import (
    ExtractParams,
    IntelligenceExtract,
    artifact_to_text,
    build_extract_analysis,
    build_extract_messages,
)
from media_engine.runtime.jsonschema import load_schema

BACKEND_NAME = "gemini"
BACKEND_VERSION = "1.0.0"


def _require_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY env var not set. Get a key at "
            "https://aistudio.google.com/apikey and `export GEMINI_API_KEY=...`."
        )
    return key


def _gemini_text_sync(
    *, api_key: str, model: str, system: str | None, user: str,
    temperature: float, max_tokens: int,
) -> tuple[str, dict[str, Any]]:
    try:
        genai: Any = importlib.import_module("google.genai")
        types: Any = importlib.import_module("google.genai.types")
    except ImportError as e:
        raise RuntimeError(
            "google-genai is not installed. Install with: "
            "uv sync --extra vlm-cloud"
        ) from e
    client = genai.Client(
        api_key=api_key, http_options=types.HttpOptions(timeout=300_000)
    )
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_tokens,
    )
    text = ""
    usage_meta: Any = None
    for chunk in client.models.generate_content_stream(
        model=model,
        contents=[types.Content(
            role="user", parts=[types.Part.from_text(text=user)]
        )],
        config=config,
    ):
        if getattr(chunk, "text", None):
            text += chunk.text
        if getattr(chunk, "usage_metadata", None):
            usage_meta = chunk.usage_metadata
    usage: dict[str, Any] = {}
    if usage_meta is not None:
        in_tok = getattr(usage_meta, "prompt_token_count", 0) or 0
        out_tok = getattr(usage_meta, "candidates_token_count", 0) or 0
        usage = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": getattr(usage_meta, "total_token_count", 0) or 0,
            "cost_cents": estimate_cost_cents(model, in_tok, out_tok),
        }
    return text, usage


@register_backend
class GeminiExtractBackend(Backend):
    op_name = "intelligence.extract"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(env=["GEMINI_API_KEY"])

    async def extract_invoke(
        self,
        source: AnyArtifact,
        params: ExtractParams,
        ctx: OperationContext,
    ) -> tuple[str, dict[str, Any]]:
        """Run the model and return ``(raw_text, usage)`` — no persistence.

        ``execute`` materializes an Analysis from this; ``intelligence.
        analyze`` calls it per window WITHOUT persisting (so it doesn't
        write orphan per-window files into the permanent store)."""
        api_key = _require_api_key()
        schema = load_schema(params.schema_def)
        content = artifact_to_text(source)
        system, user = build_extract_messages(
            params=params, schema=schema, content=content
        )
        return await asyncio.to_thread(
            _gemini_text_sync,
            api_key=api_key,
            model=params.model,
            system=system,
            user=user,
            temperature=params.temperature,
            max_tokens=params.max_tokens,
        )

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ExtractParams)
        source = inputs[0]
        text, usage = await self.extract_invoke(source, params, ctx)
        return [
            build_extract_analysis(
                source=source,
                params=params,
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                workdir_path=ctx.workdir,
                storage=ctx.storage,
                raw_text=text,
                usage=usage,
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return IntelligenceExtract().cost_estimate(inputs, params)


__all__ = ["GeminiExtractBackend"]
