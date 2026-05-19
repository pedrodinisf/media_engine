"""``claude`` backend for ``intelligence.extract`` (Anthropic Messages API)."""

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

BACKEND_NAME = "claude"
BACKEND_VERSION = "1.0.0"


def _require_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY env var not set. Get a key at "
            "https://console.anthropic.com/ and `export ANTHROPIC_API_KEY=...`."
        )
    return key


def _claude_sync(
    *, api_key: str, model: str, system: str | None, user: str,
    temperature: float, max_tokens: int,
) -> tuple[str, dict[str, Any]]:
    try:
        anthropic: Any = importlib.import_module("anthropic")
    except ImportError as e:
        raise RuntimeError(
            "anthropic is not installed. Install with: "
            "uv sync --extra vlm-cloud"
        ) from e
    client = anthropic.Anthropic(api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": user}],
    }
    if system:
        kwargs["system"] = system
    msg: Any = client.messages.create(**kwargs)
    text = "".join(
        getattr(block, "text", "") for block in msg.content
        if getattr(block, "type", None) == "text"
    )
    in_tok = getattr(msg.usage, "input_tokens", 0) or 0
    out_tok = getattr(msg.usage, "output_tokens", 0) or 0
    usage = {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
        "cost_cents": estimate_cost_cents(model, in_tok, out_tok),
    }
    return text, usage


@register_backend
class ClaudeExtractBackend(Backend):
    op_name = "intelligence.extract"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(env=["ANTHROPIC_API_KEY"])

    async def extract_invoke(
        self,
        source: AnyArtifact,
        params: ExtractParams,
        ctx: OperationContext,
    ) -> tuple[str, dict[str, Any]]:
        """Run the model and return ``(raw_text, usage)`` — no persistence
        (see GeminiExtractBackend.extract_invoke)."""
        api_key = _require_api_key()
        schema = load_schema(params.schema_def)
        content = artifact_to_text(source)
        system, user = build_extract_messages(
            params=params, schema=schema, content=content
        )
        return await asyncio.to_thread(
            _claude_sync,
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


__all__ = ["ClaudeExtractBackend"]
