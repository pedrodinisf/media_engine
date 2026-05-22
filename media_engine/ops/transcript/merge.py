"""``transcript.merge`` — collapse adjacent transcript segments.

Gap-based merger. Consecutive segments with the **same speaker** that
sit within ``gap_threshold_sec`` of each other (and don't blow past
``max_chars``) collapse into a single segment carrying the earliest
``start`` and latest ``end``. Speaker change, an over-threshold gap,
or the char cap forces a boundary — producing the longer-context
segments downstream LLM analysis prefers.

Segments without timestamps (``speakered_txt`` parses) treat the gap as
zero, so the merger reduces to "same-speaker run" collapsing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

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

OP_NAME = "transcript.merge"
OP_VERSION = "1.0.0"


class MergeParams(BaseModel):
    gap_threshold_sec: float = 2.0
    max_chars: int = 2000


def _gap(prev_end: float | None, next_start: float | None) -> float:
    if prev_end is None or next_start is None:
        return 0.0
    return max(0.0, next_start - prev_end)


def _flush(
    out: list[dict[str, Any]],
    buf_texts: list[str],
    speaker: Any,
    start: float | None,
    end: float | None,
) -> None:
    if not buf_texts:
        return
    out.append(
        {
            "start": start,
            "end": end,
            "speaker_id": speaker,
            "text": " ".join(buf_texts).strip(),
        }
    )


def merge_segments(
    segments: list[dict[str, Any]],
    *,
    gap_threshold_sec: float,
    max_chars: int,
) -> list[dict[str, Any]]:
    """Pure-function gap-based merge. Exposed for direct unit testing."""
    if not segments:
        return []
    merged: list[dict[str, Any]] = []
    buf_texts: list[str] = []
    buf_speaker: Any = segments[0].get("speaker_id")
    buf_start: float | None = segments[0].get("start")
    buf_end: float | None = segments[0].get("end")
    buf_chars = 0
    for seg in segments:
        speaker = seg.get("speaker_id")
        start = seg.get("start")
        end = seg.get("end")
        text = str(seg.get("text") or "")
        speaker_change = speaker != buf_speaker
        too_long = buf_chars + len(text) > max_chars
        gap_too_wide = _gap(buf_end, start) > gap_threshold_sec
        if buf_texts and (speaker_change or too_long or gap_too_wide):
            _flush(merged, buf_texts, buf_speaker, buf_start, buf_end)
            buf_texts = [text]
            buf_speaker = speaker
            buf_start = start
            buf_end = end
            buf_chars = len(text)
        else:
            buf_texts.append(text)
            buf_end = end if end is not None else buf_end
            buf_chars += len(text)
    _flush(merged, buf_texts, buf_speaker, buf_start, buf_end)
    return merged


@register_op
class TranscriptMerge(Operation):
    """Merge adjacent same-speaker transcript segments by gap + char cap."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = (Kind.Transcript,)
    output_kinds = (Kind.Transcript,)
    params_model = MergeParams

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        import json

        assert isinstance(params, MergeParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Transcript):
            raise ValueError(
                f"transcript.merge expects exactly one Transcript input, "
                f"got {[a.kind for a in inputs]}"
            )
        src: Transcript = inputs[0]
        merged = merge_segments(
            src.segments,
            gap_threshold_sec=params.gap_threshold_sec,
            max_chars=params.max_chars,
        )

        derived_id = compute_derived_artifact_id(
            kind=Kind.Transcript,
            op_name=OP_NAME,
            op_version=OP_VERSION,
            backend_name=None,
            backend_version=None,
            params=params,
            input_ids=[src.id],
        )
        full_text = " ".join(s["text"] for s in merged if s["text"])
        payload = {
            "text": full_text,
            "segments": merged,
            "language": src.language,
            "model": src.model,
            "merged_from": src.id,
        }
        tmp = ctx.workdir / f"merged-{derived_id[:12]}.json"
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
        # Single linear pass — cheap regardless of segment count.
        return CostEstimate(local_seconds=0.1)
