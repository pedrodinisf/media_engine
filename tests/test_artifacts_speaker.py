"""Round-trip + discriminated-union tests for the Phase-7 speaker artifacts.

Locks the mandatory new-Kind registration touch-points: the ``AnyArtifact``
union reconstructs the right subclass, and cache ``upsert``→``get`` round-trips
preserve kind + payload (i.e. ``_KIND_TO_CLASS`` is wired).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from media_engine.artifacts import (
    AnyArtifact,
    Kind,
    SpeakerEmbedding,
    SpeakerProfile,
)
from media_engine.runtime.engine import Engine

_ADAPTER: TypeAdapter[AnyArtifact] = TypeAdapter(AnyArtifact)


def _speaker_embedding(tmp_path: Path) -> SpeakerEmbedding:
    p = tmp_path / "emb.json"
    p.write_text("{}")
    return SpeakerEmbedding(
        id="a" * 64,
        path=p,
        metadata={
            "turns": [
                {"speaker_id": "SPEAKER_00", "start": 0.0, "end": 1.5,
                 "vector": [0.1, 0.2, 0.3]},
                {"speaker_id": "SPEAKER_01", "start": 1.5, "end": 3.0,
                 "vector": [0.4, 0.5, 0.6]},
            ],
            "model": "pyannote/embedding",
            "dimensions": 3,
        },
        created_at=datetime.now(UTC),
    )


def _speaker_profile(tmp_path: Path) -> SpeakerProfile:
    p = tmp_path / "prof.json"
    p.write_text("{}")
    return SpeakerProfile(
        id="b" * 64,
        path=p,
        metadata={
            "speaker_id": "Speaker_ab12cd34",
            "centroid": [0.25, 0.35, 0.45],
            "member_ids": ["a" * 64],
            "member_count": 2,
            "model": "pyannote/embedding",
        },
        created_at=datetime.now(UTC),
    )


def test_kinds_registered() -> None:
    assert Kind.SpeakerEmbedding.value == "speaker_embedding"
    assert Kind.SpeakerProfile.value == "speaker_profile"


def test_speaker_embedding_properties(tmp_path: Path) -> None:
    art = _speaker_embedding(tmp_path)
    assert art.kind is Kind.SpeakerEmbedding
    assert len(art.turns) == 2
    assert art.turns[0]["vector"] == [0.1, 0.2, 0.3]
    assert art.model == "pyannote/embedding"
    assert art.dimensions == 3


def test_speaker_profile_properties(tmp_path: Path) -> None:
    art = _speaker_profile(tmp_path)
    assert art.kind is Kind.SpeakerProfile
    assert art.speaker_id == "Speaker_ab12cd34"
    assert art.centroid == [0.25, 0.35, 0.45]
    assert art.member_ids == ["a" * 64]
    assert art.member_count == 2


@pytest.mark.parametrize("factory", [_speaker_embedding, _speaker_profile])
def test_discriminated_union_reconstructs_subclass(tmp_path, factory) -> None:
    art = factory(tmp_path)
    dumped = art.model_dump(mode="json")
    back = _ADAPTER.validate_python(dumped)
    assert type(back) is type(art)
    assert back.kind is art.kind


@pytest.mark.parametrize("factory", [_speaker_embedding, _speaker_profile])
def test_cache_round_trip(engine: Engine, tmp_path, factory) -> None:
    art = factory(tmp_path)
    engine.cache.upsert_artifact(art)
    got = engine.cache.get_artifact(art.id, namespace="default")
    assert got is not None
    assert type(got) is type(art)
    assert got.kind is art.kind
    assert got.metadata == art.metadata
