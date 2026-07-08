"""Self-managed SQLite fingerprint store for Phase-7 acoustic speaker identity.

Mirrors the ``search/semantic.db`` sidecar precedent — a plain SQLite file the
speaker backends own via ``CREATE TABLE IF NOT EXISTS`` (no alembic, no optional
dep) — but **fixes the known omission**: this table carries a ``namespace``
column so per-namespace purge works (``semantic.db`` leaks across namespaces
because it lacks one). It also stores the running centroid + ``member_count`` so
``speakers.cluster`` can update a profile as a running mean when it reconciles a
new cluster to an existing voice.

The file lives at ``permanent_store/speakers/fingerprints.db``. Vectors are
stored as ``float32`` blobs via :mod:`media_engine.backends._vec`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from media_engine.backends._vec import cosine, l2_normalize, pack, unpack

_SCHEMA = """
CREATE TABLE IF NOT EXISTS speaker_profiles (
    speaker_id   TEXT NOT NULL,
    namespace    TEXT NOT NULL,
    model        TEXT,
    dims         INTEGER NOT NULL,
    centroid     BLOB NOT NULL,
    member_count INTEGER NOT NULL,
    label        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (speaker_id, namespace)
);
CREATE INDEX IF NOT EXISTS idx_speaker_ns ON speaker_profiles(namespace);
"""


@dataclass(frozen=True)
class StoredProfile:
    """One persisted voice fingerprint (the evolving centroid, not a snapshot)."""

    speaker_id: str
    namespace: str
    model: str | None
    centroid: list[float]
    member_count: int
    label: str | None


def store_path(perm_store: Path) -> Path:
    """Path to the fingerprint DB. Pure — no filesystem side effects, so an
    ``exists()`` existence check never accidentally creates the directory."""
    return perm_store / "speakers" / "fingerprints.db"


def connect(perm_store: Path) -> sqlite3.Connection:
    path = store_path(perm_store)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def list_profiles(conn: sqlite3.Connection, namespace: str) -> list[StoredProfile]:
    cur = conn.execute(
        "SELECT speaker_id, namespace, model, dims, centroid, member_count, label "
        "FROM speaker_profiles WHERE namespace = ?",
        (namespace,),
    )
    out: list[StoredProfile] = []
    for sid, ns, model, dims, blob, count, label in cur.fetchall():
        out.append(
            StoredProfile(
                speaker_id=sid,
                namespace=ns,
                model=model,
                centroid=unpack(blob, int(dims)),
                member_count=int(count),
                label=label,
            )
        )
    return out


def upsert_profile(
    conn: sqlite3.Connection,
    profile: StoredProfile,
    *,
    now: datetime | None = None,
) -> None:
    """Insert or replace a profile (centroid is stored L2-normed)."""
    ts = (now or datetime.now(UTC)).isoformat()
    centroid = l2_normalize(profile.centroid)
    # created_at preserved on replace of an existing row; updated_at always bumps.
    existing = conn.execute(
        "SELECT created_at FROM speaker_profiles WHERE speaker_id = ? AND namespace = ?",
        (profile.speaker_id, profile.namespace),
    ).fetchone()
    created_at = existing[0] if existing else ts
    conn.execute(
        "INSERT OR REPLACE INTO speaker_profiles "
        "(speaker_id, namespace, model, dims, centroid, member_count, label, "
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            profile.speaker_id,
            profile.namespace,
            profile.model,
            len(centroid),
            pack(centroid),
            profile.member_count,
            profile.label,
            created_at,
            ts,
        ),
    )
    conn.commit()


def search(
    conn: sqlite3.Connection,
    query_vector: list[float],
    namespace: str,
    top_k: int,
) -> list[tuple[StoredProfile, float]]:
    """Brute-force cosine NN over the namespace's profiles, higher-is-better."""
    scored = [
        (p, cosine(query_vector, p.centroid))
        for p in list_profiles(conn, namespace)
    ]
    scored.sort(key=lambda r: r[1], reverse=True)
    return scored[:top_k]


def delete_namespace(conn: sqlite3.Connection, namespace: str) -> int:
    """Drop every profile in one namespace. Returns rows deleted."""
    cur = conn.execute(
        "DELETE FROM speaker_profiles WHERE namespace = ?", (namespace,)
    )
    conn.commit()
    return cur.rowcount


def purge_namespace(perm_store: Path, namespace: str) -> int:
    """Convenience wrapper: open the store, delete one namespace, close."""
    if not store_path(perm_store).exists():
        return 0
    conn = connect(perm_store)
    try:
        return delete_namespace(conn, namespace)
    finally:
        conn.close()


__all__ = [
    "StoredProfile",
    "connect",
    "delete_namespace",
    "list_profiles",
    "purge_namespace",
    "search",
    "store_path",
    "upsert_profile",
]
