"""Tests for ``speakers.cluster`` — real HDBSCAN over synthetic unit vectors.

No model needed: we hand-author two well-separated voice clusters and assert
clustering, stable-id minting, cache behaviour, and (with storage enabled)
reconcile-reuse across a simulated second run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from media_engine.artifacts import Kind, SpeakerEmbedding, SpeakerProfile
from media_engine.backends import _speaker_store as store
from media_engine.ops.speakers.cluster import ClusterParams, SpeakersCluster
from media_engine.runtime.engine import Engine

pytestmark = pytest.mark.needs_hdbscan

try:
    import hdbscan  # type: ignore[import-not-found]  # noqa: F401
    HDBSCAN_AVAILABLE = True
except ImportError:
    HDBSCAN_AVAILABLE = False

skip_no_hdbscan = pytest.mark.skipif(
    not HDBSCAN_AVAILABLE, reason="hdbscan required (uv sync --extra cluster)"
)


def _embedding(
    tmp_path: Path, art_id: str, vectors: list[tuple[str, list[float]]]
) -> SpeakerEmbedding:
    p = tmp_path / f"{art_id[:8]}.json"
    p.write_text("{}")
    turns = [
        {"speaker_id": sid, "start": float(i), "end": float(i) + 2.0, "vector": v}
        for i, (sid, v) in enumerate(vectors)
    ]
    return SpeakerEmbedding(
        id=art_id, path=p,
        metadata={"turns": turns, "model": "pyannote/embedding",
                  "dimensions": len(vectors[0][1])},
        created_at=datetime.now(UTC),
    )


def _two_voice_embedding(tmp_path: Path, art_id: str) -> SpeakerEmbedding:
    # Voice A near [1,0,0], Voice B near [0,1,0]; several turns each so HDBSCAN
    # (min_cluster_size=2) forms two clusters.
    vecs = [
        ("SPEAKER_00", [0.98, 0.02, 0.0]),
        ("SPEAKER_00", [0.97, 0.03, 0.01]),
        ("SPEAKER_00", [0.99, 0.01, 0.0]),
        ("SPEAKER_01", [0.02, 0.98, 0.0]),
        ("SPEAKER_01", [0.03, 0.97, 0.01]),
        ("SPEAKER_01", [0.01, 0.99, 0.0]),
    ]
    return _embedding(tmp_path, art_id, vecs)


def test_op_invariants() -> None:
    assert SpeakersCluster.name == "speakers.cluster"
    assert SpeakersCluster.input_kinds == (Kind.SpeakerEmbedding,)
    assert SpeakersCluster.variadic_inputs is True
    assert SpeakersCluster.output_kinds == (Kind.SpeakerProfile,)
    assert SpeakersCluster.default_backend == "hdbscan"
    assert "backend" not in ClusterParams.model_fields


@skip_no_hdbscan
async def test_clusters_two_voices(engine: Engine, tmp_path: Path) -> None:
    emb = _two_voice_embedding(tmp_path, "a" * 64)
    engine.cache.upsert_artifact(emb)
    profiles = await engine.run("speakers.cluster", inputs=[emb.id])
    assert len(profiles) == 2
    assert all(isinstance(p, SpeakerProfile) for p in profiles)
    assert all(p.speaker_id.startswith("Speaker_") for p in profiles)
    # Distinct stable ids for the two voices.
    assert len({p.speaker_id for p in profiles}) == 2


@skip_no_hdbscan
async def test_cache_hit_on_rerun_storage_off(
    engine: Engine, tmp_path: Path, mocker
) -> None:
    emb = _two_voice_embedding(tmp_path, "b" * 64)
    engine.cache.upsert_artifact(emb)
    p1 = await engine.run("speakers.cluster", inputs=[emb.id])
    from media_engine.backends.cluster.hdbscan import HdbscanClusterBackend
    spy = mocker.spy(HdbscanClusterBackend, "execute")
    p2 = await engine.run("speakers.cluster", inputs=[emb.id])
    assert spy.call_count == 0  # storage off → deterministic ids → cache hit
    assert {p.id for p in p1} == {p.id for p in p2}


@skip_no_hdbscan
async def test_param_change_yields_new_ids(engine: Engine, tmp_path: Path) -> None:
    emb = _two_voice_embedding(tmp_path, "c" * 64)
    engine.cache.upsert_artifact(emb)
    a = await engine.run("speakers.cluster", inputs=[emb.id], min_cluster_size=2)
    b = await engine.run("speakers.cluster", inputs=[emb.id], min_cluster_size=3)
    # min_cluster_size=3 still forms the two 3-member clusters, but the param is
    # part of the cache key, so the artifact ids differ.
    assert {p.id for p in a} != {p.id for p in b}


@skip_no_hdbscan
async def test_storage_off_writes_nothing(engine: Engine, tmp_path: Path) -> None:
    emb = _two_voice_embedding(tmp_path, "d" * 64)
    engine.cache.upsert_artifact(emb)
    await engine.run("speakers.cluster", inputs=[emb.id])
    # Default config has speaker_storage_enabled=False → no fingerprint DB.
    assert not store.store_path(engine.config.permanent_store).exists()


@skip_no_hdbscan
async def test_persist_and_reconcile_reuses_id(
    engine: Engine, tmp_path: Path
) -> None:
    # Turn storage on for this engine's config (frozen? EngineConfig is a
    # BaseSettings, mutable) so cluster persists + reconciles.
    engine.config.speaker_storage_enabled = True

    emb1 = _two_voice_embedding(tmp_path, "e" * 64)
    engine.cache.upsert_artifact(emb1)
    first = await engine.run("speakers.cluster", inputs=[emb1.id])
    ids_first = {p.speaker_id for p in first}
    assert len(ids_first) == 2

    # A *second* recording of the same two voices (slightly jittered) must
    # reconcile onto the same stable ids rather than mint new ones.
    emb2 = _two_voice_embedding(tmp_path, "f" * 64)
    engine.cache.upsert_artifact(emb2)
    second = await engine.run("speakers.cluster", inputs=[emb2.id])
    ids_second = {p.speaker_id for p in second}
    assert ids_second == ids_first
    assert any(p.metadata.get("reused") for p in second)

    # Fingerprint DB holds exactly the two voices, member_count grew.
    conn = store.connect(engine.config.permanent_store)
    profiles = store.list_profiles(conn, engine.config.namespace)
    conn.close()
    assert len(profiles) == 2
    assert all(p.member_count >= 6 for p in profiles)  # 3 + 3 turns each
