"""``mlx-lm`` backend for ``intelligence.extract`` (local, Apple Silicon).

The model+tokenizer are kept warm in ``ctx.model_pool`` (keyed by model
id) so repeated extracts in a pipeline don't reload weights. The blocking
``mlx_lm.generate`` call runs in a thread so the daemon loop stays free.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact
from media_engine.backends import Backend, BackendRequirements, register_backend
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.intelligence.extract import (
    ExtractParams,
    IntelligenceExtract,
    artifact_to_text,
    build_extract_analysis,
    build_extract_messages,
)
from media_engine.runtime.jsonschema import load_schema

BACKEND_NAME = "mlx-lm"
BACKEND_VERSION = "1.0.0"


def _load_mlx_lm(model_name: str) -> dict[str, Any]:
    try:
        mlx_lm: Any = importlib.import_module("mlx_lm")
    except ImportError as e:
        raise RuntimeError(
            "mlx-lm is not installed. Install with: uv sync --extra llm-mlx"
        ) from e
    model, tokenizer = mlx_lm.load(model_name)
    return {"mlx_lm": mlx_lm, "model": model, "tokenizer": tokenizer}


def _format_prompt(tokenizer: Any, system: str | None, user: str) -> str:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    apply = getattr(tokenizer, "apply_chat_template", None)
    if apply is not None:
        out: Any = apply(messages, add_generation_prompt=True, tokenize=False)
        return str(out)
    prefix = f"{system}\n\n" if system else ""
    return f"{prefix}{user}"


def _generate_sync(
    bundle: dict[str, Any], *, system: str | None, user: str, max_tokens: int
) -> str:
    mlx_lm: Any = bundle["mlx_lm"]
    model: Any = bundle["model"]
    tokenizer: Any = bundle["tokenizer"]
    prompt = _format_prompt(tokenizer, system, user)
    out: Any = mlx_lm.generate(
        model, tokenizer, prompt=prompt, max_tokens=max_tokens, verbose=False
    )
    return str(out)


@register_backend
class MlxLmExtractBackend(Backend):
    op_name = "intelligence.extract"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(
        services=["mlx-lm"], hardware=["apple_silicon"], min_memory_gb=8.0
    )

    async def extract_invoke(
        self,
        source: AnyArtifact,
        params: ExtractParams,
        ctx: OperationContext,
    ) -> tuple[str, dict[str, Any]]:
        """Run the local model and return ``(raw_text, usage)`` — no
        persistence (see GeminiExtractBackend.extract_invoke)."""
        schema = load_schema(params.schema_def)
        content = artifact_to_text(source)
        system, user = build_extract_messages(
            params=params, schema=schema, content=content
        )

        cache_key = f"mlx-lm:{params.model}"
        if ctx.model_pool is not None:
            bundle = await asyncio.to_thread(
                ctx.model_pool.get_or_load,
                cache_key,
                lambda: _load_mlx_lm(params.model),
            )
        else:
            bundle = await asyncio.to_thread(_load_mlx_lm, params.model)

        text = await asyncio.to_thread(
            _generate_sync,
            bundle,
            system=system,
            user=user,
            max_tokens=params.max_tokens,
        )
        usage: dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_cents": 0.0,
        }
        return text, usage

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


__all__ = ["MlxLmExtractBackend"]
