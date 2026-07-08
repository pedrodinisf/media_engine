"""``pyannote`` backend for ``speakers.embed_voice``.

Wraps ``pyannote.audio``'s speaker-embedding model (``pyannote/embedding``,
MPS-accelerated on Apple Silicon). Sync library → ``asyncio.to_thread``. The
``Inference`` object is loaded lazily and cached in ``ctx.model_pool`` under
``speaker-embed:<model>`` for warm reuse.

For each diarization turn we crop the audio to the turn's window and embed it,
yielding one vector per turn. Turns shorter than ``params.min_turn_seconds``
are skipped (too little signal for a stable fingerprint).

Optional dep: install via ``uv sync --extra diarize`` (same pyannote 4.x that
``audio.diarize`` uses). Requires ``HF_TOKEN`` for the gated model download.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Audio, Diarization
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.speakers.embed_voice import (
    OP_NAME,
    EmbedVoiceParams,
    build_speaker_embedding_artifact,
)
from media_engine.runtime.events import Progress
from media_engine.runtime.log_pump import attach_logger

BACKEND_NAME = "pyannote"
BACKEND_VERSION = "1.0.0"


def _import_pyannote() -> tuple[Any, Any, Any]:
    try:
        from pyannote.audio import Inference, Model  # type: ignore[import-not-found]  # noqa: I001
        from pyannote.core import Segment  # type: ignore[import-not-found]  # noqa: I001
    except ImportError as e:
        raise RuntimeError(
            "pyannote.audio is not installed. "
            "Install with: uv sync --extra diarize"
        ) from e
    return Model, Inference, Segment


def _auth_kwarg(from_pretrained: Any) -> str:
    """``token`` (pyannote 4.x) vs ``use_auth_token`` (3.x) for from_pretrained."""
    try:
        sig = inspect.signature(from_pretrained)
    except (TypeError, ValueError):
        return "token"
    return "token" if "token" in sig.parameters else "use_auth_token"


def _emit_progress(
    ctx: OperationContext, op_run_id: str, fraction: float, message: str
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
                phase="pyannote-embed",
            )
        )


def _load_inference_sync(model_name: str) -> Any:
    Model, Inference, _Segment = _import_pyannote()
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError(
            "HF_TOKEN env var not set. Pyannote voice embedding requires a "
            "HuggingFace token to download the gated model. Get one at "
            "https://huggingface.co/settings/tokens and `export HF_TOKEN=...`."
        )
    auth = _auth_kwarg(Model.from_pretrained)
    model: Any = Model.from_pretrained(model_name, **{auth: token})
    device: Any = None
    with contextlib.suppress(ImportError, RuntimeError):
        import torch  # type: ignore[import-not-found]
        torch_any: Any = torch
        if torch_any.backends.mps.is_available():
            device = torch_any.device("mps")
    if device is not None:
        return Inference(model, window="whole", device=device)
    return Inference(model, window="whole")


def _embed_turns_sync(
    inference: Any,
    audio_path: str,
    segments: list[dict[str, Any]],
    *,
    min_turn_seconds: float,
    start_s: float | None,
    end_s: float | None,
) -> list[dict[str, Any]]:
    _Model, _Inference, Segment = _import_pyannote()
    turns: list[dict[str, Any]] = []
    for seg in segments:
        start, end = float(seg["start"]), float(seg["end"])
        if (end - start) < min_turn_seconds:
            continue
        if start_s is not None and end <= start_s:
            continue
        if end_s is not None and start >= end_s:
            continue
        vector = inference.crop(audio_path, Segment(start, end))
        # ``crop`` with window="whole" returns a 1-D array-like; normalize
        # to a plain list of python floats for JSON storage.
        vec_list = [float(x) for x in _as_1d(vector)]
        turns.append({
            "speaker_id": str(seg["speaker_id"]),
            "start": start,
            "end": end,
            "vector": vec_list,
        })
    return turns


def _as_1d(vector: Any) -> list[Any]:
    """Flatten a numpy array / SlidingWindowFeature to a 1-D python list."""
    data = getattr(vector, "data", vector)
    tolist = getattr(data, "tolist", None)
    flat = tolist() if tolist is not None else list(data)
    # ``window="whole"`` may still hand back a (1, D) row — unwrap it.
    if flat and isinstance(flat[0], list):
        flat = flat[0]
    return flat


@register_backend
class PyannoteEmbedVoiceBackend(Backend):
    op_name = OP_NAME
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
        assert isinstance(params, EmbedVoiceParams)
        audio = next(a for a in inputs if isinstance(a, Audio))
        diar = next(a for a in inputs if isinstance(a, Diarization))

        run_id = uuid4().hex
        _emit_progress(ctx, run_id, 0.05, "loading embedding model")

        log_token = attach_logger(
            "pyannote",
            source="pyannote-embed",
            emit=ctx.emit,
            op_run_id=ctx.op_run_id or run_id,
            job_id=ctx.job_id,
        )
        try:
            cache_key = f"speaker-embed:{params.model}"
            inference = (
                await asyncio.to_thread(
                    ctx.model_pool.get_or_load,
                    cache_key,
                    lambda: _load_inference_sync(params.model),
                )
                if ctx.model_pool is not None
                else await asyncio.to_thread(_load_inference_sync, params.model)
            )

            _emit_progress(ctx, run_id, 0.30, "embedding turns")

            turns = await asyncio.to_thread(
                _embed_turns_sync,
                inference,
                str(audio.path),
                diar.segments,
                min_turn_seconds=params.min_turn_seconds,
                start_s=params.start_s,
                end_s=params.end_s,
            )

            _emit_progress(ctx, run_id, 1.0, f"{len(turns)} turns embedded")
        finally:
            log_token.detach()

        return [
            build_speaker_embedding_artifact(
                audio=audio,
                diarization=diar,
                params=params,
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                workdir_path=ctx.workdir,
                storage=ctx.storage,
                turns=turns,
                model=params.model,
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        diar = next((a for a in inputs if isinstance(a, Diarization)), None)
        n_turns = len(diar.segments) if diar is not None else 0
        return CostEstimate(local_seconds=0.05 * n_turns + 3.0)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "PyannoteEmbedVoiceBackend"]
