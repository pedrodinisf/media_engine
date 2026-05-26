"""``video.comprehend`` — Video → time-fused multimodal Analysis.

Composite of five existing ops plus a per-frame VLM fan-out:

  1. ``video.extract_audio``         — strip the audio track
  2. ``audio.transcribe_diarized``    — segments + speaker labels
  3. ``video.sample_frames``          — uniform-fps frame extraction
  4. (inline) per-frame ``frames.analyze`` fan-out via the chosen VLM
  5. (inline) merge frame descriptions + transcript turns into a
     time-sorted MarkdownArtifact
  6. ``intelligence.extract`` (structured) or ``intelligence.summarize``
     (prose) — one final SOTA-LLM call over the merged timeline

The engine has zero domain opinions; the timeline shape, default
schema, and per-style prompts live in ``_comprehend_prompts.py``
exactly so that swapping them is a profile change rather than an op
change.

RAM safety: the helper from ``audio.transcribe_diarized`` drops both
the mlx-whisper singleton and any ``pyannote:*`` slots from
``ctx.model_pool`` after the audio phase, and ``release_server()``
from the vllm-mlx backend tears the local VLM server down between
the fan-out and the final synth call (which goes to a different
provider entirely).
"""

from __future__ import annotations

import asyncio
import json
import platform
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    FrameSet,
    Kind,
    MarkdownArtifact,
    Video,
    compute_derived_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.ops.audio._models import DIARIZE_MODELS, WHISPER_MODELS
from media_engine.ops.audio.transcribe_diarized import release_audio_models
from media_engine.ops.intelligence._models import INTELLIGENCE_MODELS
from media_engine.ops.video._comprehend_prompts import (
    DEFAULT_SCHEMA,
    SYSTEM_PROMPTS,
    USER_PROMPTS,
)

# ── Params ───────────────────────────────────────────────────────────────


class ComprehendParams(BaseModel):
    # Frame sampling
    fps: float = Field(default=1.0, ge=0.1, le=8.0)
    max_frames: int = Field(default=240, ge=1, le=2000)

    # Per-frame VLM
    vlm_model: str = "mlx-community/Qwen2-VL-7B-Instruct-4bit"
    vlm_prompt: str = (
        "Describe what is visible in this frame in one sentence. Note any "
        "text, objects, people, actions."
    )
    max_concurrent_frames: int = Field(default=4, ge=1, le=32)

    # Audio
    transcribe_model: Annotated[
        str,
        Field(json_schema_extra={"enum": list(WHISPER_MODELS)}),
    ] = "mlx-community/whisper-medium-mlx"
    diarize_model: Annotated[
        str,
        Field(json_schema_extra={"enum": list(DIARIZE_MODELS)}),
    ] = "pyannote/speaker-diarization-3.1"
    language: str | None = None
    num_speakers: int | None = None

    # Final synthesis
    synth_model: Annotated[
        str,
        Field(json_schema_extra={"enum": list(INTELLIGENCE_MODELS)}),
    ] = "gemini-2.5-pro"
    style: Literal[
        "general", "explainer", "lecture", "interview", "tutorial"
    ] = "general"
    output_kind: Literal["structured", "prose"] = "structured"
    synth_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    synth_max_tokens: int = Field(default=4096, ge=128, le=32768)

    # Optional time-window slicing (passed to transcribe + sample)
    start_s: float | None = Field(default=None, ge=0.0)
    end_s: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _check_range(self) -> ComprehendParams:
        if (
            self.start_s is not None
            and self.end_s is not None
            and self.end_s <= self.start_s
        ):
            raise ValueError(
                f"end_s must be > start_s "
                f"(got start={self.start_s}, end={self.end_s})"
            )
        return self


# ── Helpers ───────────────────────────────────────────────────────────────


def _is_mlx_model(model_id: str) -> bool:
    return model_id.startswith("mlx-community/")


def _format_ts(t_sec: float) -> str:
    m = int(t_sec // 60)
    s = t_sec - m * 60
    return f"{m:02d}:{s:05.2f}"


def _build_single_frame_frameset(
    *,
    parent: FrameSet,
    position: int,
    ctx: OperationContext,
) -> FrameSet:
    """Persist a 1-frame FrameSet manifest for the VLM fan-out.

    Content-addressed by (parent_frameset_id, position, fps): two
    different parents with the same single frame still produce
    distinct artifacts, and re-runs of an identical composite cache-hit
    naturally (B.0 cross-check).
    """
    frame_ids = list(parent.metadata.get("frame_ids", []))
    original_indices = list(
        parent.metadata.get("original_indices", range(len(frame_ids)))
    )
    fid = str(frame_ids[position])
    orig_idx = int(original_indices[position])
    params_dict: dict[str, Any] = {
        "parent_frameset_id": parent.id,
        "position": position,
        "fps": parent.metadata.get("fps"),
    }
    derived_id = compute_derived_artifact_id(
        kind=Kind.FrameSet,
        op_name="video.comprehend._single_frame",
        op_version="1.0.0",
        backend_name=None,
        backend_version=None,
        params=params_dict,
        input_ids=[parent.id],
    )
    payload: dict[str, Any] = {
        "frame_ids": [fid],
        "original_indices": [orig_idx],
        "fps": parent.metadata.get("fps"),
        "parent_frameset_id": parent.id,
        "parent_position": position,
    }
    tmp = ctx.workdir / f"single-frame-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dest = ctx.storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)
    return FrameSet(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=(parent.id,),
        created_at=datetime.now(UTC),
    )


def _frame_timestamp(
    *,
    position: int,
    fps: float | None,
    start_s: float | None,
) -> float:
    """Reconstruct the wall-clock timestamp for an extracted frame."""
    base = (position / fps) if fps else float(position)
    return base + (start_s or 0.0)


def _build_timeline_markdown(
    *,
    frame_entries: list[tuple[float, str]],
    transcript_segments: list[dict[str, Any]],
) -> str:
    """Render `[t=mm:ss.s] FRAME: …` and `[t=…] <spk>: …` lines sorted by t.

    The merged document is what the final SOTA-LLM call sees — its
    structure is the contract between Phase B's two halves (the
    grounding signal vs. the synthesis prompt).
    """
    rows: list[tuple[float, str]] = []
    for t_sec, desc in frame_entries:
        rows.append((t_sec, f"[t={_format_ts(t_sec)}] FRAME: {desc}"))
    for seg in transcript_segments:
        try:
            start = float(seg.get("start", 0.0))
        except (TypeError, ValueError):
            start = 0.0
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        speaker = str(seg.get("speaker_id", "UNKNOWN"))
        rows.append((start, f"[t={_format_ts(start)}] {speaker}: {text}"))
    rows.sort(key=lambda r: r[0])
    return "\n".join(line for _, line in rows) + "\n"


# ── The op ────────────────────────────────────────────────────────────────


@register_op
class VideoComprehend(Operation):
    """Time-fuse frame VLM + speaker-diarized transcript via one SOTA-LLM call."""

    name = "video.comprehend"
    version = "1.0.0"
    input_kinds = (Kind.Video,)
    output_kinds = (Kind.Analysis,)
    params_model = ComprehendParams
    # Composite: every sub-op already bills its own spend. Without this
    # the cost ledger would double-count.
    records_cost = False
    delegates_to = (
        "video.extract_audio",
        "audio.transcribe_diarized",
        "video.sample_frames",
        "frames.analyze",
        "intelligence.extract",
        "intelligence.summarize",
    )
    declared_resources = ("apple_neural_engine",)

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ComprehendParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Video):
            raise ValueError(
                f"video.comprehend expects exactly one Video input, "
                f"got {[a.kind for a in inputs]}"
            )
        if ctx.run_op is None:
            raise RuntimeError(
                "video.comprehend requires ctx.run_op (call via Engine.run, "
                "not Operation.run directly)."
            )
        # Capture into a local — closures below need a narrowed type.
        run_op = ctx.run_op
        video: Video = inputs[0]

        # Hardware gate: the only local VLM backend right now is vllm-mlx
        # (Apple Silicon). Fail fast with a clear message instead of
        # silently trying to start the server on x86 Linux.
        if _is_mlx_model(params.vlm_model) and platform.machine() != "arm64":
            raise RuntimeError(
                f"vlm_model={params.vlm_model!r} requires the vllm-mlx "
                "backend (Apple Silicon). On Linux, use a gemini-prefixed "
                "vlm_model or wait for the openai-compat backend (deferred)."
            )

        # Frame-budget gate. fps × effective duration > max_frames is
        # almost always the user being sloppy with fps for a long video;
        # refuse instead of fanning out 2000 cloud calls.
        effective_duration = self._effective_duration(video, params)
        if effective_duration is not None:
            projected = int(effective_duration * params.fps)
            if projected > params.max_frames:
                raise ValueError(
                    f"fps × duration ({projected} frames) exceeds max_frames "
                    f"({params.max_frames}). Lower fps to "
                    f"≤{(params.max_frames / effective_duration):.2f} or "
                    "raise max_frames explicitly."
                )

        # ── Audio ──
        [audio] = await run_op(
            "video.extract_audio", inputs=[video.id]
        )

        td_kwargs: dict[str, Any] = {
            "transcribe_model": params.transcribe_model,
            "diarize_model": params.diarize_model,
        }
        if params.language is not None:
            td_kwargs["language"] = params.language
        if params.num_speakers is not None:
            td_kwargs["num_speakers"] = params.num_speakers
        if params.start_s is not None:
            td_kwargs["start_s"] = params.start_s
        if params.end_s is not None:
            td_kwargs["end_s"] = params.end_s
        [transcript] = await run_op(
            "audio.transcribe_diarized", inputs=[audio.id], **td_kwargs
        )

        # Free audio models before VLM fan-out (~3 GB whisper + ~2 GB
        # pyannote). The helper is a no-op for whichever wasn't loaded.
        release_audio_models(ctx)

        # ── Frames ──
        # Forward the time window so sample_frames extracts only the
        # selected segment. Without this the audio side would be
        # transcript-of-[start_s,end_s] while the visual side would
        # describe the whole video — the merged timeline would be
        # misaligned and the LLM would correlate the wrong things.
        sf_kwargs: dict[str, Any] = {"fps": params.fps}
        if params.start_s is not None:
            sf_kwargs["start_s"] = params.start_s
        if params.end_s is not None:
            sf_kwargs["end_s"] = params.end_s
        [frameset] = await run_op(
            "video.sample_frames",
            inputs=[video.id],
            **sf_kwargs,
        )
        assert isinstance(frameset, FrameSet)

        n_frames = frameset.frame_count
        if n_frames > params.max_frames:
            raise ValueError(
                f"sampled frames ({n_frames}) exceeds max_frames "
                f"({params.max_frames}); refusing to fan-out. Lower fps or "
                "raise max_frames."
            )

        # ── Per-frame VLM fan-out ──
        sem = asyncio.Semaphore(params.max_concurrent_frames)
        fps_eff = frameset.metadata.get("fps") or params.fps

        async def _analyze_one(position: int) -> tuple[float, str]:
            single_fs = _build_single_frame_frameset(
                parent=frameset, position=position, ctx=ctx
            )
            # Per the B-008 routing rule, we pass model= (which the
            # router uses to pick the backend) but NOT a hard backend=
            # override. Defer entirely to frames.analyze's model-prefix
            # selector.
            async with sem:
                analysis_outs = await run_op(
                    "frames.analyze",
                    inputs=[single_fs.id],
                    prompt=params.vlm_prompt,
                    model=params.vlm_model,
                )
            analysis = analysis_outs[0]
            text = ""
            data: Any = analysis.metadata.get("data") if hasattr(analysis, "metadata") else None
            if isinstance(data, dict):
                text = str(data.get("text", ""))  # type: ignore[arg-type]
            elif isinstance(data, str):
                text = data
            # Read the offset from the FrameSet metadata, not from
            # params — that way wall-clock timestamps come straight
            # from the artifact's own provenance and stay correct if
            # sample_frames ever slices via a different mechanism.
            t_sec = _frame_timestamp(
                position=position,
                fps=fps_eff,
                start_s=frameset.metadata.get("start_s") or params.start_s,
            )
            return (t_sec, text.strip())

        frame_entries: list[tuple[float, str]] = await asyncio.gather(
            *[_analyze_one(i) for i in range(n_frames)]
        )

        # Free the vllm-mlx server (if that's the backend that ran) so
        # the SOTA-LLM call doesn't fight it for RAM.
        if _is_mlx_model(params.vlm_model):
            try:
                from media_engine.backends.video_multimodal.vllm_mlx import (
                    release_server,
                )
            except ImportError:
                pass  # backend optional dep not installed → nothing to free
            else:
                release_server(ctx)

        # ── Merge timeline → MarkdownArtifact ──
        transcript_segments = list(transcript.metadata.get("segments", []))
        # Guard: if both the per-frame analyses and the transcript are
        # empty (silent video with all-blank frame descriptions), the
        # synth call would receive an empty document — wasteful and the
        # model would hallucinate. Fail loudly with a tunable hint.
        non_empty_frames = [(t, d) for t, d in frame_entries if d.strip()]
        if not non_empty_frames and not transcript_segments:
            raise RuntimeError(
                "video.comprehend produced an empty timeline (no usable "
                "frame descriptions and no transcript segments). Check the "
                "input video has audio + visible content, or lower fps so "
                "frames are sampled where motion is."
            )
        timeline_text = _build_timeline_markdown(
            frame_entries=list(frame_entries),
            transcript_segments=transcript_segments,
        )
        timeline_input_ids = [
            video.id, transcript.id, frameset.id,
        ]
        timeline_id = compute_derived_artifact_id(
            kind=Kind.MarkdownArtifact,
            op_name="video.comprehend._timeline",
            op_version="1.0.0",
            backend_name=None,
            backend_version=None,
            params={"fps": params.fps, "style": params.style},
            input_ids=timeline_input_ids,
        )
        tmp_md = ctx.workdir / f"timeline-{timeline_id[:12]}.md"
        tmp_md.write_text(timeline_text, encoding="utf-8")
        dest_md = ctx.storage.store_file(tmp_md, timeline_id, ".md")
        tmp_md.unlink(missing_ok=True)
        timeline_md = MarkdownArtifact(
            id=timeline_id,
            path=dest_md,
            metadata={
                "title": f"video.comprehend timeline ({params.style})",
                "fps": params.fps,
                "n_frames": n_frames,
                "n_segments": len(transcript_segments),
                "source_video_id": video.id,
                "source_transcript_id": transcript.id,
                "source_frameset_id": frameset.id,
            },
            derived_from=tuple(timeline_input_ids),
            created_at=datetime.now(UTC),
        )

        # ── Final synthesis ──
        system_prompt = SYSTEM_PROMPTS[params.style]
        user_prompt = USER_PROMPTS[params.style]

        if params.output_kind == "structured":
            synth_outs = await run_op(
                "intelligence.extract",
                inputs=[timeline_md.id],
                prompt=user_prompt,
                system_prompt=system_prompt,
                schema_def=DEFAULT_SCHEMA,
                model=params.synth_model,
                temperature=params.synth_temperature,
                max_tokens=params.synth_max_tokens,
            )
        else:
            synth_outs = await run_op(
                "intelligence.summarize",
                inputs=[timeline_md.id],
                focus=user_prompt,
                system_prompt=system_prompt,
                model=params.synth_model,
                temperature=params.synth_temperature,
                max_tokens=params.synth_max_tokens,
            )
        synth: Analysis = synth_outs[0]

        # Re-key under our own derived id so the composite has a stable,
        # cache-friendly artifact id whose lineage carries the full
        # multi-step chain.
        all_inputs = [
            video.id,
            transcript.id,
            frameset.id,
            timeline_md.id,
            synth.id,
        ]
        derived_id = compute_derived_artifact_id(
            kind=Kind.Analysis,
            op_name=self.name,
            op_version=self.version,
            backend_name=None,
            backend_version=None,
            params=params,
            input_ids=all_inputs,
        )
        # Copy the synth analysis payload under the new id so the catalog
        # has a single Analysis representing the composite (rather than
        # asking operators to chase the sub-op output).
        payload: dict[str, Any] = {
            "data": synth.metadata.get("data"),
            "model": params.synth_model,
            "style": params.style,
            "output_kind": params.output_kind,
            "fps": params.fps,
            "n_frames": n_frames,
            "n_segments": len(transcript_segments),
            "source_video_id": video.id,
            "source_transcript_id": transcript.id,
            "source_frameset_id": frameset.id,
            "source_timeline_md_id": timeline_md.id,
            "source_synth_analysis_id": synth.id,
            "vlm_model": params.vlm_model,
            "transcribe_model": params.transcribe_model,
            "diarize_model": params.diarize_model,
        }
        tmp_out = ctx.workdir / f"comprehend-{derived_id[:12]}.json"
        tmp_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        dest = ctx.storage.store_file(tmp_out, derived_id, ".json")
        tmp_out.unlink(missing_ok=True)
        return [
            Analysis(
                id=derived_id,
                path=dest,
                metadata=payload,
                derived_from=tuple(all_inputs),
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        # Composite cost estimate: sum the dominant local components +
        # the cloud synth call. Per-frame VLM cost depends on backend
        # (mlx → local seconds; gemini → cents). The engine's DAG
        # cost-preview path drills into sub-ops for the exact number;
        # here we give a defensible total at op level.
        assert isinstance(params, ComprehendParams)
        if not inputs:
            return CostEstimate()
        video = inputs[0]
        local_seconds = 0.0
        cloud_cents = 0.0
        tokens_out = 0
        if isinstance(video, Video) and video.duration is not None:
            effective = self._effective_duration_from_video(video, params)
            n_frames = min(int((effective or 0) * params.fps), params.max_frames)
            # extract_audio + transcribe_diarized are the local cost floor.
            local_seconds += (effective or 0) * 0.05  # extract_audio
            local_seconds += (effective or 0) * 0.5 + 5.0  # transcribe_diarized
            # vllm-mlx per-frame fan-out is local; gemini is cloud.
            if _is_mlx_model(params.vlm_model):
                local_seconds += 10.0 + n_frames * 1.5
            else:
                from media_engine.backends._pricing import estimate_cost_cents

                cloud_cents += estimate_cost_cents(
                    params.vlm_model, n_frames * 258, 256
                )
            # Final synthesis on a SOTA model.
            from media_engine.backends._pricing import estimate_cost_cents

            cloud_cents += estimate_cost_cents(
                params.synth_model, 2000, params.synth_max_tokens
            )
            tokens_out = params.synth_max_tokens
            return CostEstimate(
                local_seconds=local_seconds,
                cloud_cents=cloud_cents,
                tokens_in=0,
                tokens_out=tokens_out,
            )
        return CostEstimate(local_seconds=60.0)

    # ── internals ──

    def _effective_duration(
        self, video: Video, params: ComprehendParams
    ) -> float | None:
        return self._effective_duration_from_video(video, params)

    @staticmethod
    def _effective_duration_from_video(
        video: Video, params: ComprehendParams
    ) -> float | None:
        if video.duration is None:
            return None
        start = params.start_s or 0.0
        end = params.end_s if params.end_s is not None else video.duration
        return max(end - start, 0.0)


__all__ = ["ComprehendParams", "VideoComprehend"]
