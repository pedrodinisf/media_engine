"""Text-shaped artifacts: Transcript, Diarization, OCRText, Chunks, Markdown, Document, WebPage.

Subclass accessors are read-only views over the untyped ``metadata`` dict.
Phase 0/1 keeps the dict format flexible; Phase 3 will tighten to nested
Pydantic sub-models once the shapes stabilize.
"""

from __future__ import annotations

from typing import Any, Literal

from .base import Artifact, Kind


class Transcript(Artifact):
    kind: Literal[Kind.Transcript] = Kind.Transcript

    @property
    def segments(self) -> list[dict[str, Any]]:
        return list(self.metadata.get("segments", []))

    @property
    def language(self) -> str | None:
        v = self.metadata.get("language")
        return str(v) if v is not None else None

    @property
    def model(self) -> str | None:
        v = self.metadata.get("model")
        return str(v) if v is not None else None


class Diarization(Artifact):
    kind: Literal[Kind.Diarization] = Kind.Diarization

    @property
    def segments(self) -> list[dict[str, Any]]:
        return list(self.metadata.get("segments", []))

    @property
    def num_speakers(self) -> int | None:
        v = self.metadata.get("num_speakers")
        return int(v) if v is not None else None


class OCRText(Artifact):
    kind: Literal[Kind.OCRText] = Kind.OCRText

    @property
    def regions(self) -> list[dict[str, Any]]:
        return list(self.metadata.get("regions", []))


class Chunks(Artifact):
    kind: Literal[Kind.Chunks] = Kind.Chunks

    @property
    def chunks(self) -> list[dict[str, Any]]:
        return list(self.metadata.get("chunks", []))


class MarkdownArtifact(Artifact):
    kind: Literal[Kind.MarkdownArtifact] = Kind.MarkdownArtifact

    @property
    def title(self) -> str | None:
        v = self.metadata.get("title")
        return str(v) if v is not None else None


class Document(Artifact):
    kind: Literal[Kind.Document] = Kind.Document

    @property
    def page_count(self) -> int | None:
        v = self.metadata.get("page_count")
        return int(v) if v is not None else None

    @property
    def title(self) -> str | None:
        v = self.metadata.get("title")
        return str(v) if v is not None else None


class WebPage(Artifact):
    kind: Literal[Kind.WebPage] = Kind.WebPage

    @property
    def url(self) -> str | None:
        v = self.metadata.get("url")
        return str(v) if v is not None else None

    @property
    def title(self) -> str | None:
        v = self.metadata.get("title")
        return str(v) if v is not None else None
