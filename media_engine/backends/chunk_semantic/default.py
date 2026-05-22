"""Default ``chunk.semantic`` backend — regex sentence/paragraph splitter.

Two strategies:

- ``sentence``: split on `[.!?]\\s+`, then pack sentences into chunks
  capped at ``max_chars`` with ``overlap_chars`` of trailing context
  carried into each subsequent chunk.
- ``paragraph``: split on blank-line boundaries, pack the same way.

Each emitted chunk carries ``{text, char_start, char_end, chunk_index}``.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    MarkdownArtifact,
    Transcript,
)
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.chunk.semantic import (
    ChunkSemanticParams,
    build_chunks_artifact,
)

BACKEND_NAME = "default"
BACKEND_VERSION = "1.0.0"

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")
_PARAGRAPH_BOUNDARY = re.compile(r"\n\s*\n+")


def _extract_text(source: AnyArtifact) -> str:
    if isinstance(source, Transcript):
        # Prefer segments-joined text when present (preserves sentence boundaries).
        segments = source.metadata.get("segments")
        if segments:
            return " ".join(str(s.get("text", "")).strip() for s in segments).strip()
        return str(source.metadata.get("text", ""))
    if isinstance(source, MarkdownArtifact):
        return str(source.metadata.get("body", ""))  # MarkdownArtifact.body field
    raise TypeError(f"chunk.semantic does not support kind {source.kind}")


def _split_units(text: str, strategy: str) -> list[str]:
    if strategy == "paragraph":
        return [p.strip() for p in _PARAGRAPH_BOUNDARY.split(text) if p.strip()]
    # sentence
    parts = _SENTENCE_BOUNDARY.split(text)
    return [p.strip() for p in parts if p.strip()]


def _pack(units: list[str], max_chars: int, overlap_chars: int) -> list[dict[str, Any]]:
    """Greedily pack units into chunks of <= max_chars; carry overlap."""
    if not units:
        return []
    chunks: list[dict[str, Any]] = []
    char_cursor = 0
    current_parts: list[str] = []
    current_len = 0

    def emit() -> None:
        nonlocal current_parts, current_len
        if not current_parts:
            return
        text = " ".join(current_parts).strip()
        chunks.append({
            "text": text,
            "char_start": char_cursor - current_len,
            "char_end": char_cursor,
            "chunk_index": len(chunks),
        })

    for unit in units:
        unit_len = len(unit) + 1  # space
        if current_len + unit_len > max_chars and current_parts:
            emit()
            # Build overlap: take trailing overlap_chars from current_parts.
            joined = " ".join(current_parts)
            tail = joined[-overlap_chars:] if overlap_chars > 0 else ""
            current_parts = [tail] if tail else []
            current_len = len(tail) + (1 if tail else 0)
            char_cursor += unit_len  # advance past the new unit's space
        current_parts.append(unit)
        current_len += unit_len
        char_cursor += unit_len
    emit()
    return chunks


@register_backend
class DefaultChunkSemanticBackend(Backend):
    op_name = "chunk.semantic"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements()

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ChunkSemanticParams)
        source = inputs[0]
        text = _extract_text(source)
        units = _split_units(text, params.strategy)
        chunks = _pack(units, params.max_chars, params.overlap_chars)
        return [
            build_chunks_artifact(
                source=source,
                params=params,
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                workdir_path=ctx.workdir,
                storage=ctx.storage,
                chunks=chunks,
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=0.5)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "DefaultChunkSemanticBackend"]
