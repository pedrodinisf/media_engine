"""Tests for ``speakers.match`` — cosine lookup vs the fingerprint DB.

Pure synthetic vectors; the sqlite backend is the default and runs everywhere.
Covers invariants, empty-DB behaviour, ranking correctness, and an end-to-end
cluster→match reuse across two recordings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from media_engine.artifacts import Analysis, Kind, SpeakerEmbedding
from media_engine.backends import _speaker_store as store
from media_engine.backends._vec import l2_normalize
from media_engine.ops.speakers.match import (
    MatchParams,
    SpeakersMatch,
    rank_matches,
)
from media_engine.runtime.engine import Engine


def _unit(v: list[float]) -> list[float]:
    return l2_normalize(v)


def _query_embedding(
    tmp_path: Path, art_id: str, vecs: list[list[float]]
) -> SpeakerEmbedding:
    p = tmp_path / f"{art_id[:8]}.json"
    p.write_text("{}")
    turns = [
        {"speaker_id": "SPEAKER_00", "start": float(i), "end": float(i) + 2.0,
         "vector": v}
        for i, v in enumerate(vecs)
    ]
    return SpeakerEmbedding(
        id=art_id, path=p,
        metadata={"turns": turns, "model": "pyannote/embedding",
                  "dimensions": len(vecs[0])},
        created_at=datetime.now(UTC),
    )


# ── pure ranking ─────────────────────────────────────────────────────


def test_rank_matches_orders_by_best_turn() -> None:
    candidates = [
        ("Speaker_a", None, _unit([1.0, 0.0, 0.0])),
        ("Speaker_b", None, _unit([0.0, 1.0, 0.0])),
    ]
    q = [_unit([0.9, 0.1, 0.0]), _unit([0.2, 0.1, 0.0])]
    out = rank_matches(q, candidates, top_k=5, min_similarity=0.0)
    assert out[0]["speaker_id"] == "Speaker_a"
    assert out[0]["score"] > out[1]["score"]


def test_rank_matches_filters_min_similarity() -> None:
    candidates = [("Speaker_a", None, _unit([0.0, 1.0]))]
    out = rank_matches([_unit([1.0, 0.0])], candidates, top_k=5, min_similarity=0.5)
    assert out == []


def test_rank_matches_empty_candidates() -> None:
    assert rank_matches([_unit([1.0, 0.0])], [], top_k=5, min_similarity=0.0) == []


def test_rank_matches_empty_query_never_matches() -> None:
    # Even with a permissive threshold, no query vectors → no matches (not
    # "every stored voice at score 0.0").
    candidates = [("Speaker_a", None, _unit([1.0, 0.0]))]
    assert rank_matches([], candidates, top_k=5, min_similarity=-1.0) == []


# ── op ───────────────────────────────────────────────────────────────


def test_op_invariants() -> None:
    assert SpeakersMatch.name == "speakers.match"
    assert SpeakersMatch.input_kinds == (Kind.SpeakerEmbedding,)
    assert SpeakersMatch.output_kinds == (Kind.Analysis,)
    assert SpeakersMatch.default_backend == "sqlite"
    assert "backend" not in MatchParams.model_fields


async def test_match_empty_db_returns_empty(engine: Engine, tmp_path: Path) -> None:
    q = _query_embedding(tmp_path, "a" * 64, [_unit([1.0, 0.0, 0.0])])
    engine.cache.upsert_artifact(q)
    [res] = await engine.run("speakers.match", inputs=[q.id])
    assert isinstance(res, Analysis)
    assert res.metadata["results"] == []


async def test_match_finds_seeded_profile(engine: Engine, tmp_path: Path) -> None:
    # Seed the fingerprint DB directly, then match a near-identical query.
    conn = store.connect(engine.config.permanent_store)
    store.upsert_profile(conn, store.StoredProfile(
        speaker_id="Speaker_known", namespace=engine.config.namespace,
        model="pyannote/embedding", centroid=_unit([1.0, 0.0, 0.0]),
        member_count=3, label="Alice",
    ))
    store.upsert_profile(conn, store.StoredProfile(
        speaker_id="Speaker_other", namespace=engine.config.namespace,
        model="pyannote/embedding", centroid=_unit([0.0, 1.0, 0.0]),
        member_count=3, label=None,
    ))
    conn.close()

    q = _query_embedding(tmp_path, "b" * 64, [_unit([0.95, 0.05, 0.0])])
    engine.cache.upsert_artifact(q)
    [res] = await engine.run("speakers.match", inputs=[q.id])
    results = res.metadata["results"]
    assert results[0]["speaker_id"] == "Speaker_known"
    assert results[0]["label"] == "Alice"
    assert results[0]["score"] > 0.9


async def test_match_namespace_scoped(engine: Engine, tmp_path: Path) -> None:
    conn = store.connect(engine.config.permanent_store)
    store.upsert_profile(conn, store.StoredProfile(
        speaker_id="Speaker_x", namespace="other-ns",
        model="m", centroid=_unit([1.0, 0.0, 0.0]), member_count=1, label=None,
    ))
    conn.close()
    q = _query_embedding(tmp_path, "c" * 64, [_unit([1.0, 0.0, 0.0])])
    engine.cache.upsert_artifact(q)
    # Default namespace has no profiles → no match despite other-ns having one.
    [res] = await engine.run("speakers.match", inputs=[q.id])
    assert res.metadata["results"] == []


@pytest.mark.needs_hdbscan
async def test_cluster_then_match_end_to_end(engine: Engine, tmp_path: Path) -> None:
    pytest.importorskip("hdbscan")
    engine.config.speaker_storage_enabled = True

    # Cluster a recording of two voices, persisting profiles.
    vecs = [
        ("S0", [0.98, 0.02, 0.0]), ("S0", [0.97, 0.03, 0.0]), ("S0", [0.99, 0.01, 0.0]),
        ("S1", [0.02, 0.98, 0.0]), ("S1", [0.03, 0.97, 0.0]), ("S1", [0.01, 0.99, 0.0]),
    ]
    p = tmp_path / "emb.json"
    p.write_text("{}")
    emb = SpeakerEmbedding(
        id="e" * 64, path=p,
        metadata={"turns": [
            {"speaker_id": s, "start": float(i), "end": float(i) + 2.0, "vector": v}
            for i, (s, v) in enumerate(vecs)
        ], "model": "pyannote/embedding", "dimensions": 3},
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(emb)
    profiles = await engine.run("speakers.cluster", inputs=[emb.id])
    voice_a_id = next(
        pr.speaker_id for pr in profiles
        if pr.centroid[0] > pr.centroid[1]  # the [1,0,0]-ish voice
    )

    # A fresh recording of voice A must match voice A's stable id.
    q = _query_embedding(tmp_path, "f" * 64, [_unit([0.96, 0.04, 0.0])])
    engine.cache.upsert_artifact(q)
    [res] = await engine.run("speakers.match", inputs=[q.id])
    assert res.metadata["results"][0]["speaker_id"] == voice_a_id
