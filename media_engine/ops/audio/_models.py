"""Curated model ids surfaced as JSON Schema enums for the Web UI.

The audio ops' ``model`` fields stay typed as plain ``str`` so existing
CLI / REST callers can still pass off-list HuggingFace repo ids (community
mirrors, fp32 / q4 quantizations, .en English-only variants, …). The
``Annotated[str, Field(json_schema_extra={"enum": list(...)})]`` pattern
injects these tuples into the generated JSON Schema **without** activating
Pydantic's enum-enforcement validator — so the Web UI's ``SchemaForm``
renders a ``<select>`` while the server still accepts any string.

Power users who want a model outside the curated set just pass it
directly to ``med run audio.transcribe --param model=…``; the only
restriction is in the dropdown affordance.

When mlx-community / pyannote ship new variants, edit the tuples here —
the Run-panel dropdown updates on the next schema fetch. No frontend
change needed.
"""

from __future__ import annotations

# Curated mlx-community whisper variants, smallest → largest.
# Verified against ``https://huggingface.co/api/models?author=mlx-community&search=whisper``
# at v0.6.2. Excludes ``-mlx-4bit``, ``-mlx-fp32``, ``.en-mlx``
# quantization / language variants to keep the dropdown short; power
# users can still pass those via the CLI.
WHISPER_MODELS: tuple[str, ...] = (
    "mlx-community/whisper-tiny-mlx",
    "mlx-community/whisper-base-mlx",
    "mlx-community/whisper-small-mlx",
    "mlx-community/whisper-medium-mlx",
    "mlx-community/whisper-large-v3-mlx",
    "mlx-community/whisper-large-v3-turbo",
)

# pyannote speaker-diarization variants. 3.1 is the current default;
# 3.0 stays in the list as a fallback in case the licence flow on 3.1
# fails for someone (different gated model on HF).
DIARIZE_MODELS: tuple[str, ...] = (
    "pyannote/speaker-diarization-3.1",
    "pyannote/speaker-diarization-3.0",
)


__all__ = ["DIARIZE_MODELS", "WHISPER_MODELS"]
