"""Analysis-shaped artifacts: Analysis, SessionAnalysis, Embedding."""

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
