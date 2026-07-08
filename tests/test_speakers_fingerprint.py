"""Unit tests for the pure Phase-7 fingerprint logic + the SQLite store.

These are the load-bearing determinism guarantees behind "same voice = same
stable id", so they run with hand-authored vectors — no model, no marker.
"""

from __future__ import annotations

import math
from pathlib import Path

from media_engine.backends import _speaker_store as store
from media_engine.backends._vec import cosine, l2_normalize
from media_engine.ops.speakers._fingerprint import (
    ExistingProfile,
    reconcile,
    running_mean,
    stable_speaker_id,
)


def _unit(v: list[float]) -> list[float]:
    return l2_normalize(v)


# ── stable_speaker_id ────────────────────────────────────────────────


def test_stable_speaker_id_is_deterministic() -> None:
    c = _unit([0.1, 0.9, 0.3])
    assert stable_speaker_id(c) == stable_speaker_id(list(c))


def test_stable_speaker_id_format() -> None:
    sid = stable_speaker_id([1.0, 0.0, 0.0])
    assert sid.startswith("Speaker_")
    assert len(sid) == len("Speaker_") + 8


def test_stable_speaker_id_differs_for_different_voices() -> None:
    assert stable_speaker_id([1.0, 0.0]) != stable_speaker_id([0.0, 1.0])


def test_stable_speaker_id_ignores_tiny_float_noise() -> None:
    a = [0.123456, 0.654321, 0.111111]
    b = [0.1234561, 0.6543209, 0.1111114]  # sub-1e-6 jitter
    assert stable_speaker_id(a) == stable_speaker_id(b)


# ── running_mean ─────────────────────────────────────────────────────


def test_running_mean_empty_returns_old() -> None:
    old = _unit([1.0, 2.0, 3.0])
    assert running_mean(old, 5, []) == old


def test_running_mean_is_unit_length() -> None:
    out = running_mean(_unit([1.0, 0.0]), 3, [[0.0, 1.0], [0.0, 1.0]])
    assert math.isclose(math.hypot(*out), 1.0, rel_tol=1e-6)


def test_running_mean_moves_toward_new_vectors() -> None:
    old = _unit([1.0, 0.0])
    # A big batch of a new direction should pull the centroid toward it.
    out = running_mean(old, 1, [[0.0, 1.0]] * 9)
    assert cosine(out, [0.0, 1.0]) > cosine(old, [0.0, 1.0])


# ── reconcile ────────────────────────────────────────────────────────


def test_reconcile_reuses_id_above_threshold() -> None:
    existing = [ExistingProfile("Speaker_known001", _unit([1.0, 0.0, 0.0]), 4)]
    # Nearly identical direction → should reuse.
    decisions = reconcile([_unit([0.99, 0.01, 0.0])], existing, threshold=0.75)
    assert decisions[0].reused is True
    assert decisions[0].speaker_id == "Speaker_known001"
    assert decisions[0].best_score >= 0.75


def test_reconcile_mints_id_below_threshold() -> None:
    existing = [ExistingProfile("Speaker_known001", _unit([1.0, 0.0, 0.0]), 4)]
    decisions = reconcile([_unit([0.0, 1.0, 0.0])], existing, threshold=0.75)
    assert decisions[0].reused is False
    assert decisions[0].speaker_id == stable_speaker_id(_unit([0.0, 1.0, 0.0]))


def test_reconcile_is_one_to_one() -> None:
    # Two new clusters both closest to the same existing profile — only the
    # best-scoring one may claim it; the other must mint.
    existing = [ExistingProfile("Speaker_shared", _unit([1.0, 0.0]), 4)]
    new = [_unit([0.95, 0.05]), _unit([0.90, 0.10])]
    decisions = reconcile(new, existing, threshold=0.75)
    reused = [d for d in decisions if d.reused]
    assert len(reused) == 1
    assert reused[0].speaker_id == "Speaker_shared"


def test_reconcile_empty_existing_mints_all() -> None:
    new = [_unit([1.0, 0.0]), _unit([0.0, 1.0])]
    decisions = reconcile(new, [], threshold=0.75)
    assert all(not d.reused for d in decisions)
    assert len({d.speaker_id for d in decisions}) == 2


# ── SQLite store ─────────────────────────────────────────────────────


def _profile(sid: str, centroid: list[float], ns: str = "default") -> store.StoredProfile:
    return store.StoredProfile(
        speaker_id=sid, namespace=ns, model="pyannote/embedding",
        centroid=centroid, member_count=1, label=None,
    )


def test_store_upsert_and_search_roundtrip(tmp_path: Path) -> None:
    conn = store.connect(tmp_path)
    store.upsert_profile(conn, _profile("Speaker_a", _unit([1.0, 0.0, 0.0])))
    store.upsert_profile(conn, _profile("Speaker_b", _unit([0.0, 1.0, 0.0])))
    hits = store.search(conn, _unit([0.9, 0.1, 0.0]), "default", top_k=2)
    assert hits[0][0].speaker_id == "Speaker_a"
    assert hits[0][1] > hits[1][1]
    conn.close()


def test_store_namespace_isolation_and_purge(tmp_path: Path) -> None:
    conn = store.connect(tmp_path)
    store.upsert_profile(conn, _profile("Speaker_a", _unit([1.0, 0.0]), ns="alice"))
    store.upsert_profile(conn, _profile("Speaker_b", _unit([0.0, 1.0]), ns="bob"))
    assert len(store.list_profiles(conn, "alice")) == 1
    assert len(store.list_profiles(conn, "bob")) == 1
    # Search is namespace-scoped.
    assert store.search(conn, _unit([1.0, 0.0]), "bob", top_k=5)[0][0].speaker_id == "Speaker_b"
    conn.close()

    deleted = store.purge_namespace(tmp_path, "alice")
    assert deleted == 1
    conn = store.connect(tmp_path)
    assert store.list_profiles(conn, "alice") == []
    assert len(store.list_profiles(conn, "bob")) == 1
    conn.close()


def test_store_upsert_updates_in_place(tmp_path: Path) -> None:
    conn = store.connect(tmp_path)
    store.upsert_profile(conn, _profile("Speaker_a", _unit([1.0, 0.0])))
    p2 = store.StoredProfile(
        speaker_id="Speaker_a", namespace="default", model="pyannote/embedding",
        centroid=_unit([0.0, 1.0]), member_count=5, label="Alice",
    )
    store.upsert_profile(conn, p2)
    profiles = store.list_profiles(conn, "default")
    assert len(profiles) == 1
    assert profiles[0].member_count == 5
    assert profiles[0].label == "Alice"
    conn.close()


def test_purge_missing_store_is_noop(tmp_path: Path) -> None:
    assert store.purge_namespace(tmp_path / "nonexistent", "default") == 0
