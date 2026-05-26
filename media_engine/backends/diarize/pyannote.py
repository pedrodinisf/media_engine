"""``pyannote`` backend for ``audio.diarize``.

Wraps ``pyannote.audio`` (MPS-accelerated on Apple Silicon). Sync library;
we use ``asyncio.to_thread``. Models are loaded lazily and cached in
``ctx.model_pool`` for warm reuse across the daemon's lifetime.

Optional dep: install via ``uv sync --extra diarize``.
Requires the ``HF_TOKEN`` env var to download the gated model on first use.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Audio
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.audio.diarize import (
    DiarizeParams,
    build_diarization_artifact,
)
from media_engine.runtime.audio_slice import maybe_slice_audio
from media_engine.runtime.events import Progress
from media_engine.runtime.log_pump import attach_logger

BACKEND_NAME = "pyannote"
BACKEND_VERSION = "1.0.0"


def _import_pyannote() -> Any:
    try:
        from pyannote.audio import Pipeline  # type: ignore[import-not-found]  # noqa: I001
    except ImportError as e:
        raise RuntimeError(
            "pyannote.audio is not installed. "
            "Install with: uv sync --extra diarize"
        ) from e
    return Pipeline  # type: ignore[no-any-return]


def _emit_progress(
    ctx: OperationContext,
    op_run_id: str,
    fraction: float,
    message: str,
) -> None:
    with contextlib.suppress(Exception):
        ctx.emit(
            Progress(
                event_id=uuid4().hex,
                op_run_id=ctx.op_run_id or op_run_id,
                job_id=ctx.job_id,
                timestamp=datetime.now(UTC),
                fraction=max(0.0, min(1.0, fraction)),
                message=message,
                phase="pyannote",
            )
        )


def _pyannote_auth_kwarg(Pipeline: Any) -> str:
    """Pick the HF-token kwarg name for the installed pyannote version.

    pyannote.audio 4.x renamed ``use_auth_token`` → ``token`` on
    ``Pipeline.from_pretrained``. Inspect the signature once so the
    same call site works against both 3.x and 4.x without a brittle
    try/except dance on every load.
    """
    import inspect

    try:
        sig = inspect.signature(Pipeline.from_pretrained)
    except (TypeError, ValueError):
        # Some bound methods can't be introspected on certain builds;
        # the 4.x name is the better default since 3.x is EOL.
        return "token"
    if "token" in sig.parameters:
        return "token"
    return "use_auth_token"


def _load_pipeline_sync(model: str) -> Any:
    Pipeline = _import_pyannote()
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError(
            "HF_TOKEN env var not set. Pyannote diarization requires a "
            "HuggingFace token to download the gated model. Get one at "
            "https://huggingface.co/settings/tokens and `export HF_TOKEN=...`."
        )
    auth_kwarg = _pyannote_auth_kwarg(Pipeline)
    pipeline: Any = Pipeline.from_pretrained(model, **{auth_kwarg: token})
    # Move to MPS on Apple Silicon if available; CPU otherwise.
    with contextlib.suppress(ImportError, RuntimeError):
        import torch  # type: ignore[import-not-found]
        torch_any: Any = torch
        if torch_any.backends.mps.is_available():
            pipeline.to(torch_any.device("mps"))
    return pipeline


def _run_diarize_sync(
    pipeline: Any,
    audio_path: str,
    *,
    num_speakers: int | None,
    min_speakers: int | None,
    max_speakers: int | None,
) -> Any:
    kwargs: dict[str, Any] = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers
    else:
        if min_speakers is not None:
            kwargs["min_speakers"] = min_speakers
        if max_speakers is not None:
            kwargs["max_speakers"] = max_speakers
    return pipeline(audio_path, **kwargs)


def _unwrap_annotation(result: Any) -> Any:
    """Reduce a pyannote Pipeline call result to the inner ``Annotation``.

    pyannote.audio 3.x returns an ``Annotation`` directly from
    ``Pipeline(...)``. pyannote.audio 4.x wraps it as
    ``DiarizeOutput(speaker_diarization=Annotation,
    exclusive_speaker_diarization=Annotation, speaker_embeddings=…)``.
    Detect by attribute: anything that has ``itertracks`` is already an
    Annotation; otherwise drill into ``speaker_diarization``. Fail loudly
    if neither path applies so a future major bump doesn't get
    silently misclassified.
    """
    if hasattr(result, "itertracks"):
        return result
    if hasattr(result, "speaker_diarization") and hasattr(
        result.speaker_diarization, "itertracks"
    ):
        return result.speaker_diarization
    raise RuntimeError(
        f"unsupported pyannote diarization result shape: {type(result).__name__}. "
        "Expected an Annotation (3.x) or DiarizeOutput (4.x). Update "
        "_unwrap_annotation if pyannote shipped a new wrapper."
    )


def _diarization_to_segments(diarization: Any) -> tuple[list[dict[str, Any]], int]:
    """Convert a pyannote Annotation (or DiarizeOutput) to a sorted list of
    ``{start, end, speaker_id}``."""
    annotation = _unwrap_annotation(diarization)
    segments: list[dict[str, Any]] = []
    speakers: set[str] = set()
    iter_tracks: Any = annotation.itertracks(yield_label=True)
    for turn, _track, speaker in iter_tracks:
        speakers.add(str(speaker))
        segments.append({
            "start": float(turn.start),
            "end": float(turn.end),
            "speaker_id": str(speaker),
        })
    segments.sort(key=lambda s: s["start"])
    return segments, len(speakers)


@register_backend
class PyannoteDiarizeBackend(Backend):
    op_name = "audio.diarize"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(
        env=["HF_TOKEN"],
        hardware=["apple_silicon"],
    )

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, DiarizeParams)
        audio = inputs[0]
        assert isinstance(audio, Audio)

        run_id = uuid4().hex
        _emit_progress(ctx, run_id, 0.05, "loading model")

        # Bridge the pyannote python logger → LogLine for the Web UI
        # Logs tab. Must detach in finally so handlers don't accumulate
        # across runs.
        log_token = attach_logger(
            "pyannote",
            source="pyannote",
            emit=ctx.emit,
            op_run_id=ctx.op_run_id or run_id,
            job_id=ctx.job_id,
        )
        try:
            cache_key = f"pyannote:{params.model}"
            pipeline = await asyncio.to_thread(
                ctx.model_pool.get_or_load,
                cache_key,
                lambda: _load_pipeline_sync(params.model),
            ) if ctx.model_pool is not None else await asyncio.to_thread(
                _load_pipeline_sync, params.model
            )

            _emit_progress(ctx, run_id, 0.30, "embedding")

            sliced_path = await asyncio.to_thread(
                maybe_slice_audio,
                str(audio.path),
                start_s=params.start_s,
                end_s=params.end_s,
                ctx=ctx,
            )

            diarization = await asyncio.to_thread(
                _run_diarize_sync,
                pipeline,
                sliced_path,
                num_speakers=params.num_speakers,
                min_speakers=params.min_speakers,
                max_speakers=params.max_speakers,
            )

            _emit_progress(ctx, run_id, 0.85, "clustering done")

            segments, num_speakers = _diarization_to_segments(diarization)

            _emit_progress(ctx, run_id, 1.0, f"{num_speakers} speakers detected")
        finally:
            log_token.detach()

        return [
            build_diarization_artifact(
                audio=audio,
                params=params,
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                workdir_path=ctx.workdir,
                storage=ctx.storage,
                segments=segments,
                num_speakers=num_speakers,
                model=params.model,
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        if not inputs:
            return CostEstimate()
        audio = inputs[0]
        if isinstance(audio, Audio) and audio.duration is not None:
            return CostEstimate(local_seconds=audio.duration * 0.2 + 5.0)
        return CostEstimate(local_seconds=15.0)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "PyannoteDiarizeBackend"]
