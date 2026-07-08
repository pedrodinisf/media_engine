"""Analysis-shaped artifacts: Analysis, SessionAnalysis, Embedding, and the
Phase-7 acoustic speaker-identity pair (SpeakerEmbedding, SpeakerProfile)."""

from __future__ import annotations

from typing import Any, Literal

from .base import Artifact, Kind


class Analysis(Artifact):
    kind: Literal[Kind.Analysis] = Kind.Analysis

    @property
    def data(self) -> dict[str, Any]:
        return dict(self.metadata.get("data", {}))


class SessionAnalysis(Artifact):
    kind: Literal[Kind.SessionAnalysis] = Kind.SessionAnalysis

    @property
    def segments(self) -> list[dict[str, Any]]:
        return list(self.metadata.get("data", []))


class Embedding(Artifact):
    kind: Literal[Kind.Embedding] = Kind.Embedding

    @property
    def vector(self) -> list[float]:
        return list(self.metadata.get("vector", []))

    @property
    def dimensions(self) -> int:
        return len(self.vector)

    @property
    def model(self) -> str | None:
        v = self.metadata.get("model")
        return str(v) if v is not None else None


class SpeakerEmbedding(Artifact):
    """Voice fingerprints for one recording — one vector per diarization turn.

    Mirrors how ``Diarization`` holds all segments in a single artifact: one
    ``SpeakerEmbedding`` per (Audio + Diarization) pair, so lineage and the
    derived-id stay clean. ``turns`` is a list of
    ``{speaker_id, start, end, vector}`` dicts (per-recording ``speaker_id``,
    i.e. the diarization cluster label, not yet a stable cross-recording id).
    """

    kind: Literal[Kind.SpeakerEmbedding] = Kind.SpeakerEmbedding

    @property
    def turns(self) -> list[dict[str, Any]]:
        return list(self.metadata.get("turns", []))

    @property
    def model(self) -> str | None:
        v = self.metadata.get("model")
        return str(v) if v is not None else None

    @property
    def dimensions(self) -> int | None:
        v = self.metadata.get("dimensions")
        return int(v) if v is not None else None


class SpeakerProfile(Artifact):
    """A clustered voice identity with a stable cross-recording ``speaker_id``.

    ``speaker_id`` (``Speaker_<sha8>``) is a stable, mutable-state label: minted
    once for a new voice, then frozen while its stored centroid keeps evolving
    (running mean) as more recordings are clustered in. The artifact ``id``
    itself stays content-addressed per run — a re-emitted profile gets a new
    artifact id but the same ``speaker_id``. ``centroid`` is the L2-normed mean
    snapshot at this run.
    """

    kind: Literal[Kind.SpeakerProfile] = Kind.SpeakerProfile

    @property
    def speaker_id(self) -> str:
        return str(self.metadata.get("speaker_id", ""))

    @property
    def centroid(self) -> list[float]:
        return list(self.metadata.get("centroid", []))

    @property
    def member_ids(self) -> list[str]:
        return [str(x) for x in self.metadata.get("member_ids", [])]

    @property
    def member_count(self) -> int:
        return int(self.metadata.get("member_count", 0))

    @property
    def model(self) -> str | None:
        v = self.metadata.get("model")
        return str(v) if v is not None else None
