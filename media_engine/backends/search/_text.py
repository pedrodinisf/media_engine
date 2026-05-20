"""Shared "what counts as searchable text" extractor.

The fulltext index is built over text-bearing artifacts: ``Transcript``,
``MarkdownArtifact``, ``Document``, ``WebPage``, and ``Chunks``. Each
kind exposes its text through a slightly different metadata key — this
module is the single place we say "given an artifact, here's its
plaintext."

Keeping it small and dependency-free; downstream backends (FTS5 today,
pgvector tomorrow) call ``artifact_text`` and store whatever they
want — the engine never has to think about kind-specific extraction
again.
"""

from __future__ import annotations

from typing import Any, cast

from media_engine.artifacts import AnyArtifact, Kind

# Kinds we know how to index for full-text. Everything else is silently
# skipped at sync time — the index is best-effort, not authoritative.
FULLTEXT_KINDS: tuple[Kind, ...] = (
    Kind.Transcript,
    Kind.MarkdownArtifact,
    Kind.Document,
    Kind.WebPage,
    Kind.Chunks,
)


def artifact_text(artifact: AnyArtifact) -> str:
    """Return the searchable plaintext for an artifact (or ``""``).

    The mapping is deliberately permissive — if the metadata doesn't
    carry text in the expected key, we fall through to ``""`` rather
    than raising. A search backend that gets ``""`` simply doesn't
    index that row.
    """
    md: dict[str, Any] = dict(artifact.metadata or {})
    kind = artifact.kind
    if kind is Kind.Transcript:
        text = md.get("text")
        if isinstance(text, str) and text.strip():
            return text
        segs_raw: Any = md.get("segments") or []
        parts: list[str] = []
        for raw_s in cast(list[Any], segs_raw if isinstance(segs_raw, list) else []):
            seg: dict[str, Any] = (
                cast(dict[str, Any], raw_s) if isinstance(raw_s, dict) else {}
            )
            parts.append(str(seg.get("text") or ""))
        return " ".join(p for p in parts if p).strip()
    if kind is Kind.MarkdownArtifact:
        # Markdown artifacts often store the body on disk; the title
        # lives in metadata.
        try:
            return artifact.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return str(md.get("title") or "")
    if kind in (Kind.Document, Kind.WebPage):
        text = md.get("text")
        return text if isinstance(text, str) else ""
    if kind is Kind.Chunks:
        chunks_raw: Any = md.get("chunks") or []
        parts2: list[str] = []
        for raw_c in cast(list[Any], chunks_raw if isinstance(chunks_raw, list) else []):
            chunk: dict[str, Any] = (
                cast(dict[str, Any], raw_c) if isinstance(raw_c, dict) else {}
            )
            parts2.append(str(chunk.get("text") or ""))
        return " ".join(p for p in parts2 if p).strip()
    return ""


def artifact_snippet(text: str, query_terms: list[str], width: int = 160) -> str:
    """Pick a short window of ``text`` around the first matching term.

    Falls back to the leading window when no term matches — gives
    fulltext hits a useful preview without dragging in a real
    highlighter dependency.
    """
    if not text:
        return ""
    lo = text.lower()
    for term in query_terms:
        term_lo = term.lower().strip()
        if not term_lo:
            continue
        pos = lo.find(term_lo)
        if pos >= 0:
            start = max(0, pos - width // 4)
            return text[start : start + width].strip()
    return text[:width].strip()
