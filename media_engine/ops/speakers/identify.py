"""``speakers.identify`` — resolve diarization clusters to names from a CSV.

Input is a Transcript with ``speaker_id`` per segment (the kind produced
by ``audio.transcribe_diarized``). Output is a Transcript whose segments
gain a ``speaker_name: str | None`` field plus a top-level
``speaker_names`` map (``cluster_id -> resolved_name | None``) and a
``speaker_match_meta`` map carrying the confidence score per cluster so
downstream report templates can show how strong each match is.

Cluster ids are never overwritten — the resolved name lives alongside,
so SPEAKER_00 stays SPEAKER_00 even when it's also "Klaus Anybody".

Note: plan §12 originally said ``Diarization → Diarization``, but the
as-built ``Diarization`` artifact has no per-segment text (only
``{speaker_id, start, end}``), so fuzzy-matching name introductions
requires a Transcript. The deviation is ratified in plan §0 + the
architecture deviations list.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, model_validator

from media_engine.artifacts import (
    AnyArtifact,
    Kind,
    Transcript,
    compute_derived_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)

from ._speaker_db import (
    identify_speakers,
    load_speaker_db,
    text_per_cluster,
)

OP_NAME = "speakers.identify"
OP_VERSION = "1.0.0"


class IdentifyParams(BaseModel):
    """Params for ``speakers.identify``.

    ``speaker_db_sha`` is auto-derived (a model_validator hashes the CSV
    bytes) and participates in the cache key so editing the CSV
    invalidates cached results. Users never set it directly.
    """

    speaker_db: Path
    min_confidence: float = 0.7
    name_field: str = "name"
    alias_field: str = "aliases"
    intro_window_seconds: float = 30.0
    speaker_db_sha: str = ""

    @model_validator(mode="before")
    @classmethod
    def _hash_speaker_db(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = cast("dict[str, Any]", data)
        p = d.get("speaker_db")
        if p:
            pth = p if isinstance(p, Path) else Path(str(p))
            if pth.exists():
                d["speaker_db_sha"] = hashlib.sha256(
                    pth.read_bytes()
                ).hexdigest()[:16]
            else:
                # Defer the friendly FileNotFoundError to run() so the
                # error mentions the op + path; here we just mark the
                # cache key so a fix-then-rerun isn't a cache hit.
                d["speaker_db_sha"] = "missing"
        return d


@register_op
class SpeakersIdentify(Operation):
    """Resolve ``speaker_id`` clusters in a Transcript to names from a CSV."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = (Kind.Transcript,)
    output_kinds = (Kind.Transcript,)
    params_model = IdentifyParams

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, IdentifyParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Transcript):
            raise ValueError(
                f"speakers.identify expects exactly one Transcript input, "
                f"got {[a.kind for a in inputs]}"
            )
        src: Transcript = inputs[0]
        db = load_speaker_db(
            Path(params.speaker_db),
            name_field=params.name_field,
            alias_field=params.alias_field,
        )
        segments: list[dict[str, Any]] = list(
            src.metadata.get("segments", [])
        )
        cluster_texts = text_per_cluster(
            segments, intro_window_seconds=params.intro_window_seconds
        )
        matches = identify_speakers(
            cluster_texts, db, min_confidence=params.min_confidence
        )
        speaker_names: dict[str, str | None] = {
            cid: (m.canonical if m else None) for cid, m in matches.items()
        }
        match_meta: dict[str, dict[str, Any]] = {}
        for cid, m in matches.items():
            if m is None:
                match_meta[cid] = {
                    "score": 0.0,
                    "matched_candidate": None,
                    "runner_up_score": 0.0,
                }
            else:
                match_meta[cid] = {
                    "score": m.score,
                    "matched_candidate": m.matched_candidate,
                    "runner_up_score": m.runner_up_score,
                }

        out_segments: list[dict[str, Any]] = []
        for s in segments:
            sid = str(s.get("speaker_id") or "")
            new = dict(s)
            new["speaker_name"] = speaker_names.get(sid)
            out_segments.append(new)

        derived_id = compute_derived_artifact_id(
            kind=Kind.Transcript,
            op_name=OP_NAME,
            op_version=OP_VERSION,
            backend_name=None,
            backend_version=None,
            params=params,
            input_ids=[src.id],
        )
        payload: dict[str, Any] = {
            "text": src.metadata.get("text", ""),
            "segments": out_segments,
            "language": src.metadata.get("language"),
            "model": src.metadata.get("model"),
            "diarization_model": src.metadata.get("diarization_model"),
            "num_speakers": src.metadata.get("num_speakers"),
            "speaker_names": speaker_names,
            "speaker_match_meta": match_meta,
            # Pass through extra columns from the CSV — reports can render
            # e.g. position / affiliation next to the resolved name.
            "speaker_extra": {
                m.canonical: db_entry.extra
                for m in (m for m in matches.values() if m is not None)
                for db_entry in db
                if db_entry.canonical == m.canonical
            },
        }
        tmp = ctx.workdir / f"identified-{derived_id[:12]}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        dest = ctx.storage.store_file(tmp, derived_id, ".json")
        tmp.unlink(missing_ok=True)
        return [
            Transcript(
                id=derived_id,
                path=dest,
                metadata=payload,
                derived_from=(src.id,),
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        # CSV read + rapidfuzz over ≤ dozens of candidates per cluster.
        return CostEstimate(local_seconds=0.5)


__all__ = ["IdentifyParams", "OP_NAME", "OP_VERSION", "SpeakersIdentify"]
