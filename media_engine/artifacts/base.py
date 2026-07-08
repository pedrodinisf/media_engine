"""Artifact base, Kind enum, content-addressed hashing.

Source artifacts (uploaded media): id = sha256 of file bytes (streaming).
Derived artifacts: id = sha256 of canonical JSON of
{kind, op_name, op_version, backend_name, backend_version, params, sorted(input_ids)}.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


class Kind(StrEnum):
    Video = "video"
    Audio = "audio"
    Image = "image"
    FrameSet = "frameset"
    Transcript = "transcript"
    Diarization = "diarization"
    OCRText = "ocrtext"
    Chunks = "chunks"
    Embedding = "embedding"
    Analysis = "analysis"
    SessionAnalysis = "session_analysis"
    MarkdownArtifact = "markdown"
    Document = "document"
    WebPage = "webpage"
    SpeakerEmbedding = "speaker_embedding"
    SpeakerProfile = "speaker_profile"


_HASH_CHUNK_SIZE = 1024 * 1024  # 1 MB


def compute_artifact_id(file_path: Path) -> str:
    """Streaming sha256 of file bytes. Returns hex digest."""
    h = hashlib.sha256()
    with Path(file_path).open("rb") as f:
        while chunk := f.read(_HASH_CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def _json_default(o: Any) -> Any:
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, BaseModel):
        return o.model_dump(mode="json")
    if isinstance(o, set | frozenset):
        items: list[Any] = list(o)  # type: ignore[arg-type]
        return sorted(items, key=str)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization: sorted keys, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_json_default)


def canonical_params_hash(params: BaseModel | dict[str, Any]) -> str:
    """Deterministic sha256 of a Pydantic params model or dict."""
    payload = params.model_dump(mode="json") if isinstance(params, BaseModel) else params
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def compute_derived_artifact_id(
    *,
    kind: Kind,
    op_name: str,
    op_version: str,
    backend_name: str | None,
    backend_version: str | None,
    params: BaseModel | dict[str, Any],
    input_ids: Iterable[str],
) -> str:
    """sha256 over (kind, op, op_version, backend, backend_version, params, sorted(input_ids))."""
    params_dump = params.model_dump(mode="json") if isinstance(params, BaseModel) else dict(params)
    payload = {
        "kind": kind.value,
        "op_name": op_name,
        "op_version": op_version,
        "backend_name": backend_name,
        "backend_version": backend_version,
        "params": params_dump,
        "input_ids": sorted(input_ids),
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


class Artifact(BaseModel):
    """Base typed artifact. Content-addressed by sha256 id; immutable after construction.

    The ``kind`` field is declared on each subclass as ``Literal[Kind.X]`` —
    that's the discriminator for the ``AnyArtifact`` union and makes JSON
    round-trips reconstruct the correct subclass. Code that reads ``.kind``
    from an unknown artifact should use ``AnyArtifact`` (the discriminated
    union) as the parameter type.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    path: Path
    metadata: dict[str, Any] = {}
    derived_from: tuple[str, ...] = ()
    produced_by: str | None = None
    namespace: str = "default"
    created_at: datetime
