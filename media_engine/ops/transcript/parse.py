"""``transcript.parse`` — ingest a transcript file into a typed Transcript.

Three formats, one op (the plan's "one parser, one verb" shape):

* ``srt`` — SubRip subtitles (``HH:MM:SS,ms --> HH:MM:SS,ms``). No
  speakers, has timestamps. Port of davos
  ``transcript_parser.parse_srt``.
* ``speakered_txt`` — the ``[SPEAKER_XX]`` block format
  ``video_transcriber_mlx`` writes. Speakers, no timestamps. Port of
  davos ``parse_txt``.
* ``vtt`` — WebVTT (``HH:MM:SS.ms --> HH:MM:SS.ms`` plus a ``WEBVTT``
  header). Same SRT-like shape; reuses the timestamp parser.

**Identity.** The Transcript's content-addressed id is derived over
``{format, source_sha}`` — path-stable across machines for the same
file. The engine's cache key additionally includes the literal
``source_path`` (via the params model), so moving the file re-runs the
parse; a mutation under the same path *won't* invalidate that cache row
(same caveat as ``acquire.url`` — bump ``op.version`` or change a param
to force a re-parse).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Kind,
    Transcript,
    compute_artifact_id,
    compute_derived_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)

OP_NAME = "transcript.parse"
OP_VERSION = "1.0.0"

TranscriptFormat = Literal["srt", "speakered_txt", "vtt"]

# Accepts both ``,`` (SRT) and ``.`` (VTT) milliseconds separators.
_TIMESTAMP_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_SPEAKER_TAG_RE = re.compile(r"^\[([A-Z_]+\d*|UNKNOWN)\]$")
_INDEX_RE = re.compile(r"^\d+$")


class ParseParams(BaseModel):
    source_path: Path
    format: TranscriptFormat


def _ts_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _parse_cued(text: str, *, is_vtt: bool) -> list[dict[str, Any]]:
    """Shared SRT/VTT cue parser. Timestamps; no speaker info."""
    lines = text.splitlines()
    segments: list[dict[str, Any]] = []
    if is_vtt:
        # Drop the WEBVTT header + any leading metadata block.
        while lines and lines[0].strip() != "":
            if lines[0].strip().upper().startswith("WEBVTT"):
                lines.pop(0)
                continue
            # Skip ``NOTE``/``STYLE`` blocks etc.
            lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)

    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        if not raw:
            i += 1
            continue
        # SRT index line — optional in VTT; consume and continue.
        if _INDEX_RE.match(raw):
            i += 1
            continue
        m = _TIMESTAMP_RE.match(raw)
        if not m:
            i += 1
            continue
        start = _ts_to_seconds(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _ts_to_seconds(m.group(5), m.group(6), m.group(7), m.group(8))
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i].strip())
            i += 1
        segments.append(
            {
                "start": start,
                "end": end,
                "speaker_id": None,
                "text": " ".join(text_lines).strip(),
            }
        )
    return segments


def _parse_speakered_txt(text: str) -> list[dict[str, Any]]:
    """``[SPEAKER_XX]`` blocks → segments. No timestamps."""
    segments: list[dict[str, Any]] = []
    current_speaker: str | None = None
    current_text: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        m = _SPEAKER_TAG_RE.match(line)
        if m:
            if current_speaker is not None and current_text:
                segments.append(
                    {
                        "start": None,
                        "end": None,
                        "speaker_id": current_speaker,
                        "text": " ".join(current_text).strip(),
                    }
                )
            current_speaker = m.group(1)
            current_text = []
        elif line:
            current_text.append(line)
    if current_speaker is not None and current_text:
        segments.append(
            {
                "start": None,
                "end": None,
                "speaker_id": current_speaker,
                "text": " ".join(current_text).strip(),
            }
        )
    return segments


def _parse(fmt: TranscriptFormat, text: str) -> list[dict[str, Any]]:
    if fmt == "srt":
        return _parse_cued(text, is_vtt=False)
    if fmt == "vtt":
        return _parse_cued(text, is_vtt=True)
    if fmt == "speakered_txt":
        return _parse_speakered_txt(text)
    raise ValueError(f"transcript.parse: unknown format {fmt!r}")


@register_op
class TranscriptParse(Operation):
    """Ingest a transcript file (srt/speakered_txt/vtt) into a Transcript."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = ()
    output_kinds = (Kind.Transcript,)
    params_model = ParseParams

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        import json

        assert isinstance(params, ParseParams)
        if inputs:
            raise ValueError(
                f"transcript.parse takes no inputs, "
                f"got {[a.kind for a in inputs]}"
            )
        src = params.source_path
        if not src.exists():
            raise FileNotFoundError(src)

        text = src.read_text(encoding="utf-8")
        segments = _parse(params.format, text)
        source_sha = compute_artifact_id(src)

        derived_id = compute_derived_artifact_id(
            kind=Kind.Transcript,
            op_name=OP_NAME,
            op_version=OP_VERSION,
            backend_name=None,
            backend_version=None,
            params={"format": params.format, "source_sha": source_sha},
            input_ids=[],
        )
        full_text = " ".join(s["text"] for s in segments if s["text"])
        payload = {
            "text": full_text,
            "segments": segments,
            "language": None,
            "model": f"parser:{params.format}",
            "source_format": params.format,
            "source_sha": source_sha,
        }
        tmp = ctx.workdir / f"transcript-{derived_id[:12]}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        dest = ctx.storage.store_file(tmp, derived_id, ".json")
        tmp.unlink(missing_ok=True)

        return [
            Transcript(
                id=derived_id,
                path=dest,
                metadata=payload,
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        # Pure-Python parse — fast and roughly linear in file size.
        assert isinstance(params, ParseParams)
        try:
            size_mb = params.source_path.stat().st_size / (1024 * 1024)
        except OSError:
            size_mb = 0.0
        return CostEstimate(local_seconds=max(0.05, size_mb / 50.0))
