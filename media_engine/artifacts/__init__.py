"""Typed, content-addressed artifacts.

``AnyArtifact`` is the canonical Pydantic-discriminated-union type for an
artifact field that may be any subclass — used wherever the engine needs to
preserve the subclass across (de)serialization (lineage trees, cache rows,
REST/MCP payloads).
"""

from typing import Annotated, TypeAlias

from pydantic import Field

from .analysis import Analysis, Embedding, SessionAnalysis
from .base import (
    Artifact,
    Kind,
    canonical_params_hash,
    compute_artifact_id,
    compute_derived_artifact_id,
)
from .media import Audio, FrameSet, Image, Video
from .text import (
    Chunks,
    Diarization,
    Document,
    MarkdownArtifact,
    OCRText,
    Transcript,
    WebPage,
)

AnyArtifact: TypeAlias = Annotated[
    Video
    | Audio
    | Image
    | FrameSet
    | Transcript
    | Diarization
    | OCRText
    | Chunks
    | Embedding
    | Analysis
    | SessionAnalysis
    | MarkdownArtifact
    | Document
    | WebPage,
    Field(discriminator="kind"),
]

__all__ = [
    "Analysis",
    "AnyArtifact",
    "Artifact",
    "Audio",
    "Chunks",
    "Diarization",
    "Document",
    "Embedding",
    "FrameSet",
    "Image",
    "Kind",
    "MarkdownArtifact",
    "OCRText",
    "SessionAnalysis",
    "Transcript",
    "Video",
    "WebPage",
    "canonical_params_hash",
    "compute_artifact_id",
    "compute_derived_artifact_id",
]
