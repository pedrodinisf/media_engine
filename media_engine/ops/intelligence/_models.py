"""Curated model ids surfaced as JSON Schema enums for the Web UI.

Mirrors the audio-ops pattern (``media_engine/ops/audio/_models.py``):
the ``model`` field stays typed as plain ``str`` so existing CLI / REST
callers can still pass off-list ids (mlx-community quantizations,
gated cloud models, preview variants, …). The
``Annotated[str, Field(json_schema_extra={"enum": list(...)})]`` pattern
injects these tuples into the generated JSON Schema **without**
activating Pydantic's enum-enforcement validator — the Web UI's
``SchemaForm`` renders a ``<select>``, the server still accepts any
string.

When Google / Anthropic ship new model ids, edit the tuples here — the
Run-panel dropdown updates on the next schema fetch. No frontend change
needed.
"""

from __future__ import annotations

# Curated Gemini model ids. Order = recommendation order in the dropdown.
# Verified against
# ``https://generativelanguage.googleapis.com/v1beta/models`` at v0.6.2
# (May 2026): gemini-2.5-flash + gemini-2.5-pro are the current GA
# defaults; gemini-2.0-flash-001 stays as a pinned-stable fallback.
# Preview / tts / image / embedding variants are intentionally excluded
# (off-list users can pass them via ``--param model=…``).
GEMINI_MODELS: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-001",
    "gemini-2.0-flash-lite",
)

# Curated Anthropic Claude ids. Latest first; older snapshots stay for
# reproducibility (a cache key is keyed on the model id).
CLAUDE_MODELS: tuple[str, ...] = (
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-opus-4-5",
)

# Local mlx-community LLMs. Order = smallest → largest by parameter count.
# These are what ``intelligence.extract``'s router dispatches to the
# ``mlx-lm`` backend.
MLX_LM_MODELS: tuple[str, ...] = (
    "mlx-community/Llama-3.2-1B-Instruct-4bit",
    "mlx-community/Llama-3.2-3B-Instruct-4bit",
    "mlx-community/Qwen2.5-7B-Instruct-4bit",
    "mlx-community/Mistral-7B-Instruct-v0.3-4bit",
    "mlx-community/Qwen2.5-14B-Instruct-4bit",
)

# The full curated set the Run panel offers as a dropdown — concatenated
# in router-priority order (cloud first, then local). This is what the
# intelligence ops surface via json_schema_extra.
INTELLIGENCE_MODELS: tuple[str, ...] = (
    *GEMINI_MODELS,
    *CLAUDE_MODELS,
    *MLX_LM_MODELS,
)


__all__ = [
    "CLAUDE_MODELS",
    "GEMINI_MODELS",
    "INTELLIGENCE_MODELS",
    "MLX_LM_MODELS",
]
