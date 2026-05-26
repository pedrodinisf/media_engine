"""``mlx-whisper`` backend for ``audio.transcribe`` and ``audio.detect_language``.

mlx-whisper is sync; we wrap calls in ``asyncio.to_thread`` so the daemon's
event loop stays responsive. Models are loaded lazily and cached in
``ctx.model_pool`` so repeat calls within a daemon session are warm.

Optional dep: install via ``uv sync --extra transcribe-mlx``.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Audio
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.audio.detect_language import (
    DetectLanguageParams,
    build_detect_language_artifact,
)
from media_engine.ops.audio.transcribe import (
    TranscribeParams,
    build_transcript_artifact,
)
from media_engine.runtime.audio_slice import maybe_slice_audio
from media_engine.runtime.events import Progress
from media_engine.runtime.log_pump import attach_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

BACKEND_NAME = "mlx-whisper"
BACKEND_VERSION = "1.0.0"


def _import_mlx_whisper() -> Any:
    try:
        import mlx_whisper  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "mlx-whisper is not installed. "
            "Install with: uv sync --extra transcribe-mlx"
        ) from e
    return mlx_whisper


def _emit_segment_progress(
    ctx: OperationContext,
    op_run_id: str,
    artifact_id: str | None,
    fraction: float,
    message: str,
) -> None:
    with contextlib.suppress(Exception):
        ctx.emit(
            Progress(
                event_id=uuid4().hex,
                op_run_id=ctx.op_run_id or op_run_id,
                job_id=ctx.job_id,
                artifact_id=artifact_id,
                timestamp=datetime.now(UTC),
                fraction=max(0.0, min(1.0, fraction)),
                message=message,
                phase="mlx-whisper",
            )
        )


def _run_transcribe_sync(
    audio_path: str,
    params: TranscribeParams,
) -> dict[str, Any]:
    mw = _import_mlx_whisper()
    return mw.transcribe(
        audio_path,
        path_or_hf_repo=params.model,
        language=params.language,
        temperature=params.temperature,
        word_timestamps=params.word_timestamps,
    )


def _run_detect_language_sync(model: str, audio_path: str) -> dict[str, float]:
    """Return ``{lang: prob}`` from the underlying Whisper decoder.

    mlx-whisper exposes ``detect_language`` indirectly via the decoder API;
    fall back to a tiny transcribe with no decode if the helper isn't
    importable on this version.
    """
    mw = _import_mlx_whisper()

    detect: Any = getattr(mw, "detect_language", None)
    if detect is not None:  # pragma: no cover - depends on installed version
        result: Any = detect(audio_path, path_or_hf_repo=model)
        coerced = _coerce_detect_result(result)
        if coerced is not None:
            return coerced
    # Fallback: transcribe a 1-frame slice for the language field.
    res: Any = mw.transcribe(
        audio_path,
        path_or_hf_repo=model,
        condition_on_previous_text=False,
        word_timestamps=False,
    )
    return {str(res.get("language", "en")): 1.0}


def _coerce_detect_result(result: Any) -> dict[str, float] | None:
    """Normalize whatever mlx-whisper.detect_language returned into ``{lang: prob}``.

    mlx-whisper has shipped two shapes across versions: a bare ``{lang: prob}``
    dict, and a ``(detected_language, probs_dict)`` tuple. We accept either.
    """
    candidate: dict[Any, Any] | None = None
    if isinstance(result, dict):
        candidate = result  # type: ignore[assignment]
    elif isinstance(result, tuple):  # type: ignore[unreachable]
        for item in result:  # type: ignore[unreachable]
            if isinstance(item, dict):
                candidate = item  # type: ignore[assignment]
                break
    if candidate is None:
        return None
    out: dict[str, float] = {}
    for k, v in candidate.items():
        out[str(k)] = float(v)
    return out


@register_backend
class MlxWhisperTranscribeBackend(Backend):
    op_name = "audio.transcribe"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(
        services=["mlx-whisper"], hardware=["apple_silicon"]
    )

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
        derived_id_for_progress: str | None = None
        _emit_segment_progress(ctx, run_id, derived_id_for_progress, 0.0, "loading model")

        # ffmpeg-slice the audio if start_s / end_s were set; otherwise
        # this is a no-op returning the original path (no subprocess).
        sliced_path = await asyncio.to_thread(
            maybe_slice_audio,
            str(audio.path),
            start_s=params.start_s,
            end_s=params.end_s,
            ctx=ctx,
        )

        # Bridge the mlx-whisper python logger → LogLine for the Web UI
        # Logs tab. Must detach in finally so handlers don't accumulate
        # across runs.
        log_token = attach_logger(
            "mlx_whisper",
            source="mlx-whisper",
            emit=ctx.emit,
            op_run_id=ctx.op_run_id or run_id,
            job_id=ctx.job_id,
        )
        try:
            result = await asyncio.to_thread(
                _run_transcribe_sync, sliced_path, params
            )
        finally:
            log_token.detach()

        text: str = str(result.get("text", ""))
        segments_raw = list(result.get("segments", []))
        segments: list[dict[str, Any]] = []
        total = max(1, len(segments_raw))
        for i, seg in enumerate(segments_raw):
            segments.append(
                {
                    "id": seg.get("id", i),
                    "start": float(seg.get("start", 0.0)),
                    "end": float(seg.get("end", 0.0)),
                    "text": str(seg.get("text", "")),
                    "tokens": list(seg.get("tokens", [])),
                    "words": list(seg.get("words", []))
                    if params.word_timestamps
                    else [],
                }
            )
            _emit_segment_progress(
                ctx, run_id, None, (i + 1) / total,
                f"segment {i+1}/{total}",
            )

        language: str = str(result.get("language", params.language or "unknown"))
        duration: float | None = audio.duration

        transcript = build_transcript_artifact(
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
        return [transcript]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        if not inputs:
            return CostEstimate()
        audio = inputs[0]
        if isinstance(audio, Audio) and audio.duration is not None:
            return CostEstimate(local_seconds=audio.duration * 0.3)
        return CostEstimate(local_seconds=10.0)


@register_backend
class MlxWhisperDetectLanguageBackend(Backend):
    op_name = "audio.detect_language"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(
        services=["mlx-whisper"], hardware=["apple_silicon"]
    )

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, DetectLanguageParams)
        audio = inputs[0]
        assert isinstance(audio, Audio)

        # Bridge mlx-whisper logger → LogLine for the Web UI Logs tab.
        # detect_language uses the same library as transcribe, so the
        # same surface applies.
        run_id = uuid4().hex
        log_token = attach_logger(
            "mlx_whisper",
            source="mlx-whisper",
            emit=ctx.emit,
            op_run_id=ctx.op_run_id or run_id,
            job_id=ctx.job_id,
        )
        try:
            probs = await asyncio.to_thread(
                _run_detect_language_sync, params.model, str(audio.path)
            )
        finally:
            log_token.detach()
        if not probs:
            language = "unknown"
            confidence = 0.0
            alternatives: dict[str, float] = {}
        else:
            language = max(probs.items(), key=lambda kv: kv[1])[0]
            confidence = float(probs[language])
            alternatives = {k: float(v) for k, v in probs.items()}

        analysis = build_detect_language_artifact(
            audio=audio,
            params=params,
            backend_name=BACKEND_NAME,
            backend_version=BACKEND_VERSION,
            workdir_path=ctx.workdir,
            storage=ctx.storage,
            language=language,
            confidence=confidence,
            alternatives=alternatives,
        )
        return [analysis]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=2.0)


__all__ = [
    "BACKEND_NAME",
    "BACKEND_VERSION",
    "MlxWhisperDetectLanguageBackend",
    "MlxWhisperTranscribeBackend",
]
