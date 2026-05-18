"""Shared Gemini image/frames helper.

frames.analyze / frames.compare / image.describe / image.ocr (fallback) /
image.classify all make the same shape of Gemini call: N inline images +
a text prompt → streamed text + usage. Factor it once here so each
backend stays a thin adapter.

Lazy SDK import; sync call wrapped by callers via asyncio.to_thread.
Optional dep: ``uv sync --extra vlm-cloud``; requires ``GEMINI_API_KEY``.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

from media_engine.backends._pricing import estimate_cost_cents


def _import_genai() -> tuple[Any, Any]:
    try:
        genai_mod: Any = importlib.import_module("google.genai")
        types_mod: Any = importlib.import_module("google.genai.types")
    except ImportError as e:
        raise RuntimeError(
            "google-genai is not installed. Install with: "
            "uv sync --extra vlm-cloud"
        ) from e
    return genai_mod, types_mod


def require_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY env var not set. Get a key at "
            "https://aistudio.google.com/apikey and `export GEMINI_API_KEY=...`."
        )
    return key


def gemini_vision_sync(
    *,
    api_key: str,
    model: str,
    image_bytes: list[bytes],
    prompt: str,
    system_prompt: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> tuple[str, dict[str, Any]]:
    """Blocking Gemini call with inline JPEG parts. Returns (text, usage)."""
    genai, types = _import_genai()
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=300_000),
    )
    parts = [
        types.Part.from_bytes(data=b, mime_type="image/jpeg")
        for b in image_bytes
    ]
    parts.append(types.Part.from_text(text=prompt))
    gen_config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=temperature,
        max_output_tokens=max_tokens,
    )

    text = ""
    usage_meta: Any = None
    for chunk in client.models.generate_content_stream(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=gen_config,
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
