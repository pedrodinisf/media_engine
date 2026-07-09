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

# AssemblyAI cloud speech-to-text models. Namespaced with an ``assemblyai/``
# prefix so audio.transcribe's router dispatches by prefix (``assemblyai/*`` →
# the assemblyai backend; ``mlx-community/*`` → mlx-whisper). Off-list ids are
# still accepted. Both do one-call diarization; universal-3-5-pro additionally
# supports ``prompt`` + ``keyterms``. https://www.assemblyai.com/pricing
ASSEMBLYAI_MODELS: tuple[str, ...] = (
    "assemblyai/universal-3-5-pro",
    "assemblyai/universal-2",
)

# AssemblyAI per-audio-hour base rates (USD). Add-ons stack additively; we
# model diarization (+$0.02/hr) since it's the common one here.
_ASSEMBLYAI_RATES_USD_PER_HR: dict[str, float] = {
    "universal-3-5-pro": 0.21,
    "universal-2": 0.15,
}
_ASSEMBLYAI_DIARIZATION_USD_PER_HR = 0.02


def is_assemblyai_model(model: str) -> bool:
    return model.startswith("assemblyai/")


def strip_assemblyai_prefix(model: str) -> str:
    """``assemblyai/universal-2`` → ``universal-2`` (the API's speech_model id)."""
    return model.split("/", 1)[1] if is_assemblyai_model(model) else model


def assemblyai_cost_cents(
    model: str, duration_s: float | None, *, diarize: bool
) -> float:
    """Estimated AssemblyAI spend in cents for ``duration_s`` seconds of audio."""
    hours = (duration_s or 0.0) / 3600.0
    rate = _ASSEMBLYAI_RATES_USD_PER_HR.get(strip_assemblyai_prefix(model), 0.21)
    if diarize:
        rate += _ASSEMBLYAI_DIARIZATION_USD_PER_HR
    return hours * rate * 100.0


__all__ = [
    "ASSEMBLYAI_MODELS",
    "DIARIZE_MODELS",
    "WHISPER_MODELS",
    "assemblyai_cost_cents",
    "is_assemblyai_model",
    "strip_assemblyai_prefix",
]
