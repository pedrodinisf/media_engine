"""Media artifact subclasses: Video, Audio, Image, FrameSet."""

from __future__ import annotations

from typing import Literal

from .base import Artifact, Kind


class Video(Artifact):
    kind: Literal[Kind.Video] = Kind.Video

    @property
    def duration(self) -> float | None:
        v = self.metadata.get("duration")
        return float(v) if v is not None else None

    @property
    def width(self) -> int | None:
        v = self.metadata.get("width")
        return int(v) if v is not None else None

    @property
    def height(self) -> int | None:
        v = self.metadata.get("height")
        return int(v) if v is not None else None

    @property
    def codec(self) -> str | None:
        v = self.metadata.get("codec")
        return str(v) if v is not None else None

    @property
    def fps(self) -> float | None:
        v = self.metadata.get("fps")
        return float(v) if v is not None else None


class Audio(Artifact):
    kind: Literal[Kind.Audio] = Kind.Audio

    @property
    def duration(self) -> float | None:
        v = self.metadata.get("duration")
        return float(v) if v is not None else None

    @property
    def sample_rate(self) -> int | None:
        v = self.metadata.get("sample_rate")
        return int(v) if v is not None else None

    @property
    def channels(self) -> int | None:
        v = self.metadata.get("channels")
        return int(v) if v is not None else None

    @property
    def codec(self) -> str | None:
        v = self.metadata.get("codec")
        return str(v) if v is not None else None


class Image(Artifact):
    kind: Literal[Kind.Image] = Kind.Image

    @property
    def width(self) -> int | None:
        v = self.metadata.get("width")
        return int(v) if v is not None else None

    @property
    def height(self) -> int | None:
        v = self.metadata.get("height")
        return int(v) if v is not None else None


class FrameSet(Artifact):
    kind: Literal[Kind.FrameSet] = Kind.FrameSet

    @property
    def frame_ids(self) -> list[str]:
        return list(self.metadata.get("frame_ids", []))

    @property
    def frame_count(self) -> int:
        return len(self.frame_ids)

    @property
    def fps(self) -> float | None:
        v = self.metadata.get("fps")
        return float(v) if v is not None else None
