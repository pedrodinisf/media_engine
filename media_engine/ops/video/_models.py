"""Curated VLM model ids for video / frame ops, surfaced as JSON Schema enums.

Same pattern as ``intelligence/_models.py`` + ``audio/_models.py``: the
``vlm_model`` / ``model`` fields stay typed ``str`` (off-list ids still
accepted via CLI / REST) but ``Annotated[str, Field(json_schema_extra=
{"enum": list(...)})]`` injects the curated set into the generated JSON
Schema so the Web UI renders a provider-grouped ``<select>`` instead of a
free-text box.

The frame / video VLM ops route by **model prefix** — ``mlx-community/`` →
the local ``vllm-mlx`` backend (Apple Silicon), ``gemini-`` → the cloud
``gemini`` backend — so the dropdown deliberately mixes both providers.
The client's ``classifyModelProvider`` mirrors these prefixes to tag each
option local vs cloud.

When Apple / Google ship new ids, edit the tuples here — the dropdown
updates on the next schema fetch. No frontend change needed.
"""

from __future__ import annotations

# Local Apple-Silicon VLMs (vllm-mlx backend). Order = smallest → largest.
# The 2B variant fits on a 16 GB Mac alongside whisper + pyannote.
VLM_MLX_MODELS: tuple[str, ...] = (
    "mlx-community/Qwen2-VL-2B-Instruct-4bit",
    "mlx-community/Qwen2-VL-7B-Instruct-4bit",
)

# Cloud Gemini VLMs (gemini backend). Order = recommendation order.
VLM_GEMINI_MODELS: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
)

# Combined curated set — local first, then cloud (mirrors the
# on-device-preferred router priority). This is what the frame / VLM ops
# surface via json_schema_extra.
VLM_MODELS: tuple[str, ...] = (*VLM_MLX_MODELS, *VLM_GEMINI_MODELS)


__all__ = ["VLM_GEMINI_MODELS", "VLM_MLX_MODELS", "VLM_MODELS"]
