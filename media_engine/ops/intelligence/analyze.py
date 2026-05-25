"""``intelligence.analyze`` — Transcript → SessionAnalysis (per-segment).

Composite: slides a window over the transcript's segments and runs the
registered ``intelligence.extract`` backend on each window (optionally a
second ``classify`` pass), assembling one ``AnalyzedSegment`` per window.

The per-segment schema, prompt and (optional) classification taxonomy are
all profile-supplied — the engine keeps zero domain opinions. The
bundled ``analysis-full`` profile ships a generic schema (summary,
topics, entities, claims, sentiment polarity, questions); specialize
by cloning and editing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, field_validator, model_validator

from media_engine.artifacts import (
    AnyArtifact,
    Kind,
    Transcript,
    compute_derived_artifact_id,
)
from media_engine.artifacts.analysis import SessionAnalysis
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.ops.intelligence.classify import (
    CLASSIFY_SCHEMA,
    classify_prompt,
)
from media_engine.ops.intelligence.extract import (
    ExtractParams,
    IntelligenceExtract,
    _default_backend_for_model,
    finalize_extract_data,
    invoke_extract_backend,
)
from media_engine.runtime.jsonschema import load_schema


class AnalyzeParams(BaseModel):
    prompt: str = ""
    schema_def: dict[str, Any] | str
    model: str = "gemini-2.5-flash"
    system_prompt: str | None = None
    temperature: float = 0.2
    max_tokens: int = 2048
    window: int = 1  # transcript segments per analysis window
    classify_labels: list[str] | None = None
    classify_multi_label: bool = False

    @model_validator(mode="before")
    @classmethod
    def _resolve_prompt_path(cls, data: Any) -> Any:
        """Resolve ``prompt_path: <path>`` (only accepted via input dict) into
        the inline ``prompt`` field at construction time.

        We don't expose ``prompt_path`` as a real model field because the
        cache key needs to track the file's *resolved text*, not its path.
        Reading the file here means a profile that says
        ``prompt_path: profiles/foo/prompt.md`` cache-invalidates correctly
        whenever the file's contents change.
        """
        if not isinstance(data, dict):
            return data
        d = cast("dict[str, Any]", data)
        pp = d.pop("prompt_path", None)
        if pp and not d.get("prompt"):
            d["prompt"] = Path(str(pp)).read_text(encoding="utf-8")
        return d

    @model_validator(mode="after")
    def _check_prompt_set(self) -> AnalyzeParams:
        if not self.prompt:
            raise ValueError(
                "intelligence.analyze requires `prompt` or `prompt_path`."
            )
        return self

    @field_validator("window")
    @classmethod
    def _window_ge_1(cls, v: int) -> int:
        if v < 1:
            raise ValueError("window must be >= 1")
        return v


def _windows(
    segments: list[dict[str, Any]], size: int
) -> list[list[dict[str, Any]]]:
    return [segments[i : i + size] for i in range(0, len(segments), size)]


def _window_transcript(
    window: list[dict[str, Any]], idx: int, salt: str
) -> Transcript:
    text = "\n".join(str(s.get("text", "")) for s in window)
    wid = hashlib.sha256(f"{salt}:{idx}:{text}".encode()).hexdigest()
    return Transcript(
        id=wid,
        path=Path(f"/tmp/window-{wid[:12]}.json"),
        metadata={"text": text, "segments": window},
        created_at=datetime.now(UTC),
    )


@register_op
class IntelligenceAnalyze(Operation):
    """Per-segment structured analysis of a Transcript → SessionAnalysis."""

    name = "intelligence.analyze"
    # 1.1.0 — propagates `speaker_names` from the input Transcript and
    # records `speaker` per window using speaker_name > speaker_id >
    # speaker (in that order), so report templates and cross-session
    # aggregates see human-readable names when speakers.identify has run.
    version = "1.1.0"
    input_kinds = (Kind.Transcript,)
    output_kinds = (Kind.SessionAnalysis,)
    params_model = AnalyzeParams
    # Composite-by-backend: it drives the registered intelligence.extract
    # backend directly per window (not via ctx.run_op, since windows are
    # synthetic and not cached individually). The whole SessionAnalysis is
    # content-addressed + cached at this op's granularity.
    default_backend = None
    delegates_to = ("intelligence.extract",)

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, AnalyzeParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Transcript):
            raise ValueError(
                f"intelligence.analyze expects exactly one Transcript input, "
                f"got {[a.kind for a in inputs]}"
            )
        transcript = inputs[0]
        segments: list[dict[str, Any]] = list(
            transcript.metadata.get("segments", [])
        )
        if not segments:
            raise ValueError(
                "intelligence.analyze: transcript has no segments"
            )
        load_schema(params.schema_def)  # fail fast on bad schema

        backend_name = _default_backend_for_model(params.model)
        backend = BackendRegistry.get("intelligence.extract", backend_name)()

        extract_params = ExtractParams(
            prompt=params.prompt,
            schema_def=params.schema_def,
            model=params.model,
            system_prompt=params.system_prompt,
            temperature=params.temperature,
            max_tokens=params.max_tokens,
        )
        classify_params: ExtractParams | None = None
        if params.classify_labels:
            classify_params = ExtractParams(
                prompt=classify_prompt(
                    params.classify_labels, params.classify_multi_label
                ),
                schema_def=CLASSIFY_SCHEMA,
                model=params.model,
                system_prompt=params.system_prompt,
                temperature=0.0,
                max_tokens=1024,
            )

        salt = f"{transcript.id}:{params.model}"
        analyzed: list[dict[str, Any]] = []
        agg = {"input_tokens": 0, "output_tokens": 0, "cost_cents": 0.0}

        for idx, win in enumerate(_windows(segments, params.window)):
            wt = _window_transcript(win, idx, salt)
            # Non-persisting per-window extraction: invoke the backend and
            # finalize in-memory. (Calling backend.execute would write an
            # orphan per-window Analysis file into the permanent store.)
            raw, usage = await invoke_extract_backend(
                backend, wt, extract_params, ctx
            )
            seg: dict[str, Any] = {
                "window_index": idx,
                "start": win[0].get("start"),
                "end": win[-1].get("end"),
                "text": wt.metadata["text"],
                "analysis": finalize_extract_data(raw, extract_params),
            }
            # Prefer the resolved human name (from speakers.identify), fall
            # back to the diarization cluster id, then to a legacy `speaker`
            # field (kept for back-compat with hand-built transcripts).
            speaker = (
                win[0].get("speaker_name")
                or win[0].get("speaker_id")
                or win[0].get("speaker")
            )
            if speaker is not None:
                seg["speaker"] = speaker
            _accumulate(agg, usage)

            if classify_params is not None:
                c_raw, c_usage = await invoke_extract_backend(
                    backend, wt, classify_params, ctx
                )
                seg["classification"] = finalize_extract_data(
                    c_raw, classify_params
                )
                _accumulate(agg, c_usage)

            analyzed.append(seg)

        derived_id = compute_derived_artifact_id(
            kind=Kind.SessionAnalysis,
            op_name=self.name,
            op_version=self.version,
            backend_name=backend_name,
            backend_version=getattr(backend, "version", "1.0.0"),
            params=params,
            input_ids=[transcript.id],
        )
        payload: dict[str, Any] = {
            "data": analyzed,
            "model": params.model,
            "backend": backend_name,
            "window": params.window,
            "segment_count": len(segments),
            "usage": agg,
            # Pass-through speaker resolution from the input Transcript so
            # downstream report templates (report.session,
            # report.zeitgeist) can render human names without re-reading
            # the source artifact.
            "speaker_names": dict(
                transcript.metadata.get("speaker_names") or {}
            ),
        }
        tmp = ctx.workdir / f"session-analysis-{derived_id[:12]}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        dest = ctx.storage.store_file(tmp, derived_id, ".json")
        tmp.unlink(missing_ok=True)
        return [
            SessionAnalysis(
                id=derived_id,
                path=dest,
                metadata=payload,
                derived_from=(transcript.id,),
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, AnalyzeParams)
        n_segments = 0
        if inputs and isinstance(inputs[0], Transcript):
            n_segments = len(inputs[0].metadata.get("segments", []))
        n_windows = max(1, -(-n_segments // params.window)) if n_segments else 1
        per = IntelligenceExtract().cost_estimate(
            [],
            ExtractParams(
                prompt=params.prompt,
                schema_def=params.schema_def,
                model=params.model,
                max_tokens=params.max_tokens,
            ),
        )
        passes = 2 if params.classify_labels else 1
        mult = n_windows * passes
        return CostEstimate(
            local_seconds=per.local_seconds * mult,
            cloud_cents=per.cloud_cents * mult,
            tokens_in=per.tokens_in * mult,
            tokens_out=per.tokens_out * mult,
        )


def _accumulate(agg: dict[str, Any], raw: Any) -> None:
    if isinstance(raw, dict):
        usage: dict[str, Any] = cast("dict[str, Any]", raw)
        agg["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
        agg["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
        agg["cost_cents"] += float(usage.get("cost_cents", 0.0) or 0.0)


__all__ = ["AnalyzeParams", "IntelligenceAnalyze"]
