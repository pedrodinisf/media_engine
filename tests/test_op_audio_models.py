"""Curated model dropdown — JSON Schema enum + permissive Pydantic validation.

The audio ops surface a curated set of mlx-community / pyannote model
ids as JSON Schema ``enum``, which the Web UI's ``SchemaForm`` renders
as a ``<select>``. The field type stays ``str`` (not ``Literal``) so
CLI / REST callers can still pass off-list HuggingFace repo ids.
"""

from __future__ import annotations

from media_engine.ops.audio._models import (
    ASSEMBLYAI_MODELS,
    DIARIZE_MODELS,
    WHISPER_MODELS,
)
from media_engine.ops.audio.detect_language import DetectLanguageParams
from media_engine.ops.audio.diarize import DiarizeParams
from media_engine.ops.audio.transcribe import TranscribeParams
from media_engine.ops.audio.transcribe_diarized import TranscribeDiarizedParams


def test_whisper_catalog_is_non_empty_and_includes_default() -> None:
    assert len(WHISPER_MODELS) >= 4
    # The default in TranscribeParams must be a member of the catalog.
    assert TranscribeParams().model in WHISPER_MODELS
    assert DetectLanguageParams().model in WHISPER_MODELS


def test_diarize_catalog_is_non_empty_and_includes_default() -> None:
    assert len(DIARIZE_MODELS) >= 1
    assert DiarizeParams().model in DIARIZE_MODELS


def test_transcribe_schema_emits_whisper_plus_assemblyai_enum() -> None:
    schema = TranscribeParams.model_json_schema()
    enum = schema["properties"]["model"].get("enum")
    assert enum is not None, "model field must carry a JSON Schema enum"
    # Local whisper ids first, then the cloud assemblyai/* ids.
    assert tuple(enum) == (*WHISPER_MODELS, *ASSEMBLYAI_MODELS)


def test_detect_language_schema_emits_whisper_plus_assemblyai_enum() -> None:
    schema = DetectLanguageParams.model_json_schema()
    assert tuple(schema["properties"]["model"]["enum"]) == (
        *WHISPER_MODELS,
        *ASSEMBLYAI_MODELS,
    )


def test_diarize_schema_emits_diarize_enum() -> None:
    schema = DiarizeParams.model_json_schema()
    assert tuple(schema["properties"]["model"]["enum"]) == DIARIZE_MODELS


def test_transcribe_diarized_emits_both_enums() -> None:
    schema = TranscribeDiarizedParams.model_json_schema()
    assert tuple(schema["properties"]["transcribe_model"]["enum"]) == (
        *WHISPER_MODELS,
        *ASSEMBLYAI_MODELS,
    )
    assert tuple(schema["properties"]["diarize_model"]["enum"]) == DIARIZE_MODELS


def test_off_list_models_still_validate() -> None:
    """The enum is a UI affordance, not a server-side constraint.

    Power users passing community mirrors, .en variants, or quantized
    builds via the CLI must still validate — otherwise we'd silently
    break ``med run audio.transcribe --param model=...`` for anyone
    using anything other than the curated six.
    """
    p = TranscribeParams(model="mlx-community/whisper-medium.en-mlx")
    assert p.model == "mlx-community/whisper-medium.en-mlx"
    p2 = DiarizeParams(model="pyannote/some-future-variant")
    assert p2.model == "pyannote/some-future-variant"
