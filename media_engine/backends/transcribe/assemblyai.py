"""``assemblyai`` backend for ``audio.transcribe`` + ``audio.detect_language``.

AssemblyAI is a cloud speech-to-text provider that returns transcription,
speaker diarization, and language detection in a single async API call. The
SDK's ``Transcriber.transcribe`` uploads the file, submits the job, and polls
to completion — a blocking call we wrap in ``asyncio.to_thread`` so the
daemon's event loop stays responsive.

Because AssemblyAI diarizes in the same call, this backend writes
``speaker_id`` directly into each transcript segment when
``params.speaker_labels`` is set — the same segment shape
``audio.transcribe_diarized`` otherwise produces by merging whisper + pyannote.

Auth: ``ASSEMBLYAI_API_KEY``. Optional dep: ``uv sync --extra transcribe-assemblyai``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Audio
from media_engine.backends import Backend, BackendRequirements, register_backend
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.audio._models import (
    ASSEMBLYAI_PROMPT_MODELS,
    assemblyai_cost_cents,
    strip_assemblyai_prefix,
)
from media_engine.ops.audio.detect_language import (
    DetectLanguageParams,
    build_detect_language_artifact,
)
from media_engine.ops.audio.transcribe import (
    TranscribeParams,
    build_transcript_artifact,
)
from media_engine.runtime.events import Progress

BACKEND_NAME = "assemblyai"
BACKEND_VERSION = "1.0.0"

_API_KEY_ENV = "ASSEMBLYAI_API_KEY"


def _require_api_key() -> str:
    key = os.environ.get(_API_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"{_API_KEY_ENV} env var not set. Get a key at "
            "https://www.assemblyai.com/app/account and "
            f"`export {_API_KEY_ENV}=...` (or set it in Settings → Secrets)."
        )
    return key


def _import_assemblyai() -> Any:
    try:
        return importlib.import_module("assemblyai")
    except ImportError as e:
        raise RuntimeError(
            "assemblyai is not installed. "
            "Install with: uv sync --extra transcribe-assemblyai"
        ) from e


def _emit(
    ctx: OperationContext, run_id: str, fraction: float, message: str
) -> None:
    with contextlib.suppress(Exception):
        ctx.emit(
            Progress(
                event_id=uuid4().hex,
                op_run_id=ctx.op_run_id or run_id,
                job_id=ctx.job_id,
                artifact_id=None,
                timestamp=datetime.now(UTC),
                fraction=max(0.0, min(1.0, fraction)),
                message=message,
                phase="assemblyai",
            )
        )


def _keyterms(raw: str | None) -> list[str]:
    """Split the comma/newline-separated ``keyterms`` param into a list."""
    if not raw:
        return []
    parts = [t.strip() for chunk in raw.splitlines() for t in chunk.split(",")]
    return [t for t in parts if t]


def _build_config(aai: Any, params: TranscribeParams, *, detect_only: bool) -> Any:
    """Assemble an ``aai.TranscriptionConfig`` from our params."""
    cfg_kwargs: dict[str, Any] = {
        "speech_models": [strip_assemblyai_prefix(params.model)],
    }
    if params.language:
        cfg_kwargs["language_code"] = params.language
    else:
        cfg_kwargs["language_detection"] = True
    if not detect_only:
        if params.speaker_labels:
            cfg_kwargs["speaker_labels"] = True
            if params.min_speakers is not None or params.max_speakers is not None:
                cfg_kwargs["speaker_options"] = aai.SpeakerOptions(
                    min_speakers_expected=params.min_speakers,
                    max_speakers_expected=params.max_speakers,
                )
        # prompt + keyterms are only accepted by universal-3-5-pro; forwarding
        # them to universal-2 makes AssemblyAI reject the whole job.
        if strip_assemblyai_prefix(params.model) in ASSEMBLYAI_PROMPT_MODELS:
            if params.prompt:
                cfg_kwargs["prompt"] = params.prompt
            keyterms = _keyterms(params.keyterms)
            if keyterms:
                cfg_kwargs["keyterms_prompt"] = keyterms
    if params.start_s is not None:
        cfg_kwargs["audio_start_from"] = int(params.start_s * 1000)
    if params.end_s is not None:
        cfg_kwargs["audio_end_at"] = int(params.end_s * 1000)
    return aai.TranscriptionConfig(**cfg_kwargs)


def _transcribe_sync(params: TranscribeParams, audio_path: str, *, detect_only: bool) -> Any:
    aai = _import_assemblyai()
    aai.settings.api_key = _require_api_key()
    config = _build_config(aai, params, detect_only=detect_only)
    transcript = aai.Transcriber().transcribe(audio_path, config=config)
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI transcription failed: {transcript.error}")
    return transcript


def _word_dicts(words: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    items: list[Any] = list(words) if words else []
    for w in items:
        out.append(
            {
                "text": str(getattr(w, "text", "")),
                "start": float(getattr(w, "start", 0) or 0) / 1000.0,
                "end": float(getattr(w, "end", 0) or 0) / 1000.0,
                "confidence": float(getattr(w, "confidence", 0.0) or 0.0),
            }
        )
    return out


def _utterance_segments(
    utterances: Any, *, word_timestamps: bool
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for i, u in enumerate(utterances or []):
        segments.append(
            {
                "id": i,
                "start": float(getattr(u, "start", 0) or 0) / 1000.0,
                "end": float(getattr(u, "end", 0) or 0) / 1000.0,
                "text": str(getattr(u, "text", "")).strip(),
                # None speaker → "" so the transcribe_diarized composite stamps
                # it "UNKNOWN" (its per-segment speaker_id invariant).
                "speaker_id": str(getattr(u, "speaker", None) or ""),
                "words": _word_dicts(getattr(u, "words", None))
                if word_timestamps
                else [],
            }
        )
    return segments


def _sentence_segments(words: Any, *, word_timestamps: bool) -> list[dict[str, Any]]:
    """Group word-level results into sentence segments (no extra API call).

    AssemblyAI returns word timings but not sentence boundaries in the base
    payload; we split on sentence-final punctuation so the Transcript has
    timestamped segments even without diarization."""
    wds = _word_dicts(words)
    if not wds:
        return []
    segments: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = []
    for w in wds:
        cur.append(w)
        if w["text"].endswith((".", "?", "!")):
            segments.append(_flush_sentence(cur, len(segments), word_timestamps))
            cur = []
    if cur:
        segments.append(_flush_sentence(cur, len(segments), word_timestamps))
    return segments


def _flush_sentence(
    words: list[dict[str, Any]], idx: int, word_timestamps: bool
) -> dict[str, Any]:
    return {
        "id": idx,
        "start": words[0]["start"],
        "end": words[-1]["end"],
        "text": " ".join(w["text"] for w in words).strip(),
        "words": list(words) if word_timestamps else [],
    }


@register_backend
class AssemblyAITranscribeBackend(Backend):
    op_name = "audio.transcribe"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(env=[_API_KEY_ENV], services=["assemblyai"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, TranscribeParams)
        audio = inputs[0]
        assert isinstance(audio, Audio)

        run_id = uuid4().hex
        _emit(ctx, run_id, 0.0, "uploading + submitting to AssemblyAI")
        transcript = await asyncio.to_thread(
            _transcribe_sync, params, str(audio.path), detect_only=False
        )
        _emit(ctx, run_id, 1.0, "AssemblyAI transcription complete")

        if params.speaker_labels and getattr(transcript, "utterances", None):
            segments = _utterance_segments(
                transcript.utterances, word_timestamps=params.word_timestamps
            )
        else:
            segments = _sentence_segments(
                getattr(transcript, "words", None),
                word_timestamps=params.word_timestamps,
            )

        text = str(getattr(transcript, "text", "") or "")
        language = str(
            getattr(transcript, "language_code", None) or params.language or "unknown"
        )
        duration = getattr(transcript, "audio_duration", None) or audio.duration

        result = build_transcript_artifact(
            audio=audio,
            params=params,
            backend_name=BACKEND_NAME,
            backend_version=BACKEND_VERSION,
            workdir_path=ctx.workdir,
            storage=ctx.storage,
            text=text,
            segments=segments,
            language=language,
            model=params.model,
            duration=duration,
        )
        return [result]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        if not inputs:
            return CostEstimate()
        assert isinstance(params, TranscribeParams)
        audio = inputs[0]
        duration = audio.duration if isinstance(audio, Audio) else None
        return CostEstimate(
            cloud_cents=assemblyai_cost_cents(
                params.model, duration, diarize=params.speaker_labels
            )
        )


@register_backend
class AssemblyAIDetectLanguageBackend(Backend):
    # NOTE: AssemblyAI has no language-only endpoint — this submits a full
    # transcription with language_detection on, so it bills like a transcribe.
    # Prefer audio.transcribe (which already returns the detected language)
    # unless you specifically want a standalone language Analysis.
    op_name = "audio.detect_language"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(env=[_API_KEY_ENV], services=["assemblyai"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, DetectLanguageParams)
        audio = inputs[0]
        assert isinstance(audio, Audio)

        run_id = uuid4().hex
        _emit(ctx, run_id, 0.0, "detecting language via AssemblyAI")
        # Reuse the transcribe path with language_detection forced on.
        t_params = TranscribeParams(
            model=params.model, start_s=params.start_s, end_s=params.end_s
        )
        transcript = await asyncio.to_thread(
            _transcribe_sync, t_params, str(audio.path), detect_only=True
        )
        language = str(getattr(transcript, "language_code", None) or "unknown")
        confidence = float(getattr(transcript, "language_confidence", 0.0) or 0.0)

        # AssemblyAI exposes only the winning language + its confidence (no
        # per-language probability map), so alternatives is a single entry —
        # unlike the whisper backend's ranked {lang: prob} map.
        analysis = build_detect_language_artifact(
            audio=audio,
            params=params,
            backend_name=BACKEND_NAME,
            backend_version=BACKEND_VERSION,
            workdir_path=ctx.workdir,
            storage=ctx.storage,
            language=language,
            confidence=confidence,
            alternatives={language: confidence},
        )
        return [analysis]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        if not inputs:
            return CostEstimate()
        assert isinstance(params, DetectLanguageParams)
        audio = inputs[0]
        duration = audio.duration if isinstance(audio, Audio) else None
        return CostEstimate(
            cloud_cents=assemblyai_cost_cents(params.model, duration, diarize=False)
        )


__all__ = [
    "BACKEND_NAME",
    "BACKEND_VERSION",
    "AssemblyAIDetectLanguageBackend",
    "AssemblyAITranscribeBackend",
]
