"""``vllm-mlx`` backend for ``video.multimodal`` (local, Apple Silicon).

Reuses the engine's frame-extraction ops rather than reimplementing
them inline:

  video.sample_frames  →  frames.subsample  →  base64 → OpenAI chat call

So frames are content-addressed + cached: re-running with the same fps
skips extraction. The server is managed via ``ctx.server_manager`` —
started on first use, hot-swapped when a different model is requested,
health-gated on ``GET /v1/models``.

vllm-mlx is a *binary* (an OpenAI-compatible server), not a Python import;
the HTTP call uses httpx (core dep). ``BackendRequirements`` flags the
binary + Apple-Silicon hardware so health/errors are actionable.
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, FrameSet, Video
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.video.multimodal import (
    MultimodalVideoParams,
    build_multimodal_analysis_artifact,
)
from media_engine.runtime.events import Progress
from media_engine.runtime.log_pump import attach_file_tail

BACKEND_NAME = "vllm-mlx"
BACKEND_VERSION = "1.0.0"

_SERVER_NAME = "vllm-mlx"
_DEFAULT_PORT = 8000
_MAX_FRAMES = 30
_DEFAULT_FPS = 1.0
_READY_TIMEOUT_S = 180.0


def find_vllm_mlx_binary() -> str | None:
    """Locate the vllm-mlx binary (venv bin → PATH → ~/.local → ~/.cargo)."""
    candidate = os.path.join(os.path.dirname(sys.executable), "vllm-mlx")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    on_path = shutil.which("vllm-mlx")
    if on_path:
        return on_path
    for d in (os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.cargo/bin")):
        c = os.path.join(d, "vllm-mlx")
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _emit(ctx: OperationContext, run_id: str, frac: float, msg: str) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        ctx.emit(
            Progress(
                event_id=uuid4().hex,
                op_run_id=ctx.op_run_id or run_id,
                job_id=ctx.job_id,
                timestamp=datetime.now(UTC),
                fraction=max(0.0, min(1.0, frac)),
                message=msg,
                phase="vllm-mlx",
            )
        )


def _frame_timestamp(metadata: dict[str, Any], position: int, original_idx: int) -> str:
    """Reconstruct an MM:SS label for a frame.

    Scene-change FrameSets carry per-scene midpoints; uniform FrameSets use
    original_index / fps.
    """
    midpoints_raw: Any = metadata.get("scene_midpoints_sec")
    if isinstance(midpoints_raw, list) and position < len(midpoints_raw):  # type: ignore[arg-type]
        ts = float(midpoints_raw[position])  # type: ignore[index]
    else:
        fps = float(metadata.get("fps") or _DEFAULT_FPS)
        ts = original_idx / fps if fps else 0.0
    return f"{int(ts) // 60:02d}:{int(ts) % 60:02d}"


def _server_command(binary: str, model: str, port: int) -> list[str]:
    # vllm-mlx exposes an OpenAI-compatible server: `vllm-mlx serve <model>`.
    return [binary, "serve", model, "--port", str(port)]


def _ensure_server(
    ctx: OperationContext,
    model: str,
    port: int,
    run_id: str,
) -> str:
    """Ensure a vllm-mlx server is up serving ``model``. Returns base URL.

    Hot-swap: if a server is running a *different* model, stop + restart.
    """
    sm = ctx.server_manager
    if sm is None:
        raise RuntimeError(
            "video.multimodal vllm-mlx backend needs a ServerManager "
            "(run via Engine, not a bare OperationContext)."
        )
    binary = find_vllm_mlx_binary()
    if binary is None:
        raise RuntimeError(
            "vllm-mlx binary not found. Install it (e.g. "
            "`uv tool install vllm-mlx`) and ensure it's on PATH."
        )

    base_url = f"http://127.0.0.1:{port}"
    health_url = f"{base_url}/v1/models"
    health = sm.health_check(_SERVER_NAME, url=health_url)

    if health.running and health.model == model and health.healthy:
        return base_url

    # Hardware gate before a (re)start. We don't know the exact model size;
    # use a conservative 8 GB heuristic so tiny machines fail fast with a
    # clear message rather than swap-thrash.
    from media_engine.runtime.hardware import check_model_fits

    fit = check_model_fits(8.0, headroom_gb=4.0)
    if not fit.fits:
        raise RuntimeError(
            f"Refusing to load {model!r}: only {fit.available_gb:.1f} GB RAM "
            f"available, need ~12 GB (8 GB model + 4 GB headroom)."
        )

    if health.running and health.model != model:
        _emit(ctx, run_id, 0.1, f"hot-swapping server → {model}")
        sm.restart(
            _SERVER_NAME,
            _server_command(binary, model, port),
            meta={"model": model},
        )
    else:
        _emit(ctx, run_id, 0.1, f"starting vllm-mlx ({model})")
        sm.start(
            _SERVER_NAME,
            _server_command(binary, model, port),
            meta={"model": model},
        )

    def _on_progress(elapsed: float, status: str) -> None:
        _emit(ctx, run_id, 0.15, f"{status} ({elapsed:.0f}s)")

    sm.wait_until_ready(
        _SERVER_NAME,
        url=health_url,
        timeout=_READY_TIMEOUT_S,
        on_progress=_on_progress,
    )
    return base_url


def _frame_data_url(ctx: OperationContext, frame_id: str) -> str:
    path = ctx.storage.artifact_path(frame_id, ".jpg")
    data = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:image/jpeg;base64,{data}"


def _build_messages(
    ctx: OperationContext,
    frameset: FrameSet,
    params: MultimodalVideoParams,
) -> list[dict[str, Any]]:
    frame_ids = list(frameset.metadata.get("frame_ids", []))
    original_indices = list(
        frameset.metadata.get("original_indices", range(len(frame_ids)))
    )
    user_content: list[dict[str, Any]] = []
    for pos, (fid, orig) in enumerate(zip(frame_ids, original_indices, strict=False)):
        label = _frame_timestamp(frameset.metadata, pos, int(orig))
        user_content.append({"type": "text", "text": f"[Frame at {label}]"})
        user_content.append({
            "type": "image_url",
            "image_url": {"url": _frame_data_url(ctx, str(fid))},
        })
    prompt = params.prompt
    if params.additional_instructions:
        prompt += f"\n\n{params.additional_instructions}"
    user_content.append({"type": "text", "text": prompt})

    messages: list[dict[str, Any]] = []
    if params.system_prompt:
        messages.append({"role": "system", "content": params.system_prompt})
    messages.append({"role": "user", "content": user_content})
    return messages


@register_backend
class VllmMlxVideoMultimodalBackend(Backend):
    op_name = "video.multimodal"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(
        binaries=["vllm-mlx"],
        hardware=["apple_silicon"],
        min_memory_gb=12.0,
    )

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, MultimodalVideoParams)
        video = inputs[0]
        assert isinstance(video, Video)
        if ctx.run_op is None:
            raise RuntimeError(
                "video.multimodal vllm-mlx backend needs ctx.run_op "
                "(frames are produced via the video.sample_frames + "
                "frames.subsample ops). Run via Engine."
            )

        run_id = uuid4().hex
        port = _DEFAULT_PORT

        # 1. Frames via ops (content-addressed → cached on rerun).
        _emit(ctx, run_id, 0.02, "sampling frames")
        [frameset] = await ctx.run_op(
            "video.sample_frames", inputs=[video.id], fps=_DEFAULT_FPS
        )
        [reduced] = await ctx.run_op(
            "frames.subsample", inputs=[frameset.id], max_n=_MAX_FRAMES
        )
        assert isinstance(reduced, FrameSet)

        # Attach the file-tail BEFORE ensure_server so the 30-60s boot
        # phase shows up in the Logs tab (it's the slowest, noisiest
        # part). The server is detached by ServerManager (long-lived
        # across CLI invocations, log captured to a file rather than a
        # pipe) so file-tail is the only way to surface its output
        # without breaking the detached lifecycle.
        log_handle = None
        if ctx.server_manager is not None:
            log_handle = attach_file_tail(
                str(ctx.server_manager.log_path(_SERVER_NAME)),
                source="vllm-mlx",
                emit=ctx.emit,
                op_run_id=ctx.op_run_id or run_id,
                job_id=ctx.job_id,
            )

        try:
            # 2. Ensure the server is serving the requested model.
            base_url = _ensure_server(ctx, params.model, port, run_id)

            # 3. Base64 frames + OpenAI chat call.
            _emit(ctx, run_id, 0.5, "encoding frames + generating")
            messages = _build_messages(ctx, reduced, params)
            async with httpx.AsyncClient(timeout=600.0) as client:
                resp = await client.post(
                    f"{base_url}/v1/chat/completions",
                    json={
                        "model": params.model,
                        "messages": messages,
                        "temperature": params.temperature,
                        "max_tokens": params.max_tokens,
                    },
                )
                resp.raise_for_status()
                body: dict[str, Any] = resp.json()
        finally:
            if log_handle is not None:
                log_handle.cancel()
                await log_handle.aclose()

        text = (
            body.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        raw_usage: dict[str, Any] = body.get("usage", {})
        usage = {
            "input_tokens": raw_usage.get("prompt_tokens", 0),
            "output_tokens": raw_usage.get("completion_tokens", 0),
            "total_tokens": raw_usage.get("total_tokens", 0),
            "cost_cents": 0.0,  # local inference is free
            "frames_sent": len(reduced.metadata.get("frame_ids", [])),
        }
        _emit(ctx, run_id, 1.0, f"done ({len(text)} chars)")

        return [
            build_multimodal_analysis_artifact(
                video=video,
                params=params,
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                workdir_path=ctx.workdir,
                storage=ctx.storage,
                text=str(text),
                usage=usage,
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        if not inputs:
            return CostEstimate()
        video = inputs[0]
        if isinstance(video, Video) and video.duration is not None:
            # Frame sampling + local VLM inference, no cloud cost.
            return CostEstimate(local_seconds=video.duration * 0.3 + 10.0)
        return CostEstimate(local_seconds=30.0)


def release_server(ctx: OperationContext) -> bool:
    """Stop the vllm-mlx server so its RAM is freed.

    Composites that fan out a different model after a vllm-mlx phase
    (e.g. ``video.comprehend``: per-frame VLM → audio transcribe →
    Gemini synth) call this between phases to reclaim the ~8-12 GB the
    server holds. The next caller will pay the ~30-60s warm cost when
    they ask for a model again, but the rest of the pipeline gets the
    RAM back. Returns ``True`` if something was actually stopped.
    """
    if ctx.server_manager is None:
        return False
    return ctx.server_manager.stop(_SERVER_NAME)


# Public re-exports: the vllm-mlx server lifecycle + frame-encoding path is
# shared verbatim by ``backends.frames_analyze.vllm_mlx`` (same local model,
# no extraction step). Exposed as public names so that reuse doesn't reach
# into another module's privates.
DEFAULT_PORT = _DEFAULT_PORT
ensure_server = _ensure_server
frame_data_url = _frame_data_url

__all__ = [
    "BACKEND_NAME",
    "BACKEND_VERSION",
    "DEFAULT_PORT",
    "VllmMlxVideoMultimodalBackend",
    "ensure_server",
    "find_vllm_mlx_binary",
    "frame_data_url",
    "release_server",
]
