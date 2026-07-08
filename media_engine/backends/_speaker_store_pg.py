"""Postgres/pgvector implementation of the speaker fingerprint store.

Import-clean sibling of ``_speaker_store.py`` (the SQLite sidecar): the module
imports without ``psycopg``/``pgvector`` installed and lazy-imports them inside
the call path, so the pgvector match backend registers everywhere and only
needs the deps at ``execute()`` time.

The connection URL comes from ``MEDIA_ENGINE_SPEAKER_DB_URL`` (env) or the
engine's main cache URL when that is Postgres-shaped. Table name overridable via
``MEDIA_ENGINE_SPEAKER_PGV_TABLE`` (default ``speaker_profiles_pgv``). The schema
mirrors the SQLite one (``namespace`` column for per-namespace purge) with the
centroid stored as a pgvector column for native cosine distance.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

from media_engine.backends._speaker_store import StoredProfile

_TABLE = os.environ.get("MEDIA_ENGINE_SPEAKER_PGV_TABLE", "speaker_profiles_pgv")


def resolve_db_url() -> str:
    explicit = os.environ.get("MEDIA_ENGINE_SPEAKER_DB_URL")
    if explicit:
        return explicit
    from media_engine.config import EngineConfig

    cfg_url = EngineConfig.load().resolve_cache_db_url()
    if not cfg_url.startswith(("postgresql", "postgres+")):
        raise RuntimeError(
            "pgvector speaker store cannot use a SQLite cache URL; set "
            "MEDIA_ENGINE_SPEAKER_DB_URL to a Postgres connection string"
        )
    return cfg_url


def _connect(dims: int | None) -> Any:
    try:
        psycopg = importlib.import_module("psycopg")
        pgvector_psycopg = importlib.import_module("pgvector.psycopg")
    except ImportError as e:
        raise RuntimeError(
            "pgvector speaker store requires psycopg + pgvector. "
            "Install with: uv sync --extra postgres"
        ) from e
    # psycopg accepts a libpq URL; normalize the SQLAlchemy-style prefix.
    url = resolve_db_url().replace("postgresql+psycopg", "postgresql")
    conn = psycopg.connect(url)
    pgvector_psycopg.register_vector(conn)
    if dims is not None:
        ensure_schema(conn, dims)
    return conn


def ensure_schema(conn: Any, dims: int) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                speaker_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                model TEXT,
                dims INTEGER NOT NULL,
                centroid vector({dims}) NOT NULL,
                member_count INTEGER NOT NULL,
                label TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (speaker_id, namespace)
            )
            """
        )
    conn.commit()


def connect(dims: int | None = None) -> Any:
    """Open a registered connection. ``dims`` triggers schema creation."""
    return _connect(dims)


def list_profiles(conn: Any, namespace: str) -> list[StoredProfile]:
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"SELECT speaker_id, namespace, model, dims, centroid, "
                f"member_count, label FROM {_TABLE} WHERE namespace = %s",
                (namespace,),
            )
        except Exception:
            conn.rollback()
            return []
        rows = cur.fetchall()
    out: list[StoredProfile] = []
    for sid, ns, model, _dims, centroid, count, label in rows:
        out.append(
            StoredProfile(
                speaker_id=sid, namespace=ns, model=model,
                centroid=[float(x) for x in centroid],
                member_count=int(count), label=label,
            )
        )
    return out


def upsert_profile(conn: Any, profile: StoredProfile) -> None:
    from media_engine.backends._vec import l2_normalize

    centroid = l2_normalize(profile.centroid)
    ensure_schema(conn, len(centroid))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {_TABLE}
                (speaker_id, namespace, model, dims, centroid, member_count,
                 label, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (speaker_id, namespace) DO UPDATE SET
                model = EXCLUDED.model, dims = EXCLUDED.dims,
                centroid = EXCLUDED.centroid,
                member_count = EXCLUDED.member_count,
                label = EXCLUDED.label, updated_at = now()
            """,
            (
                profile.speaker_id, profile.namespace, profile.model,
                len(centroid), centroid, profile.member_count, profile.label,
            ),
        )
    conn.commit()


def delete_namespace(namespace: str) -> int:
    conn = _connect(None)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    f"DELETE FROM {_TABLE} WHERE namespace = %s", (namespace,)
                )
            except Exception:
                conn.rollback()
                return 0
            deleted = cur.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def is_configured() -> bool:
    """True when a Postgres speaker-store URL is resolvable (env or PG cache)."""
    if os.environ.get("MEDIA_ENGINE_SPEAKER_DB_URL"):
        return True
    try:
        from media_engine.config import EngineConfig

        return EngineConfig.load().resolve_cache_db_url().startswith(
            ("postgresql", "postgres+")
        )
    except Exception:
        return False


__all__ = [
    "connect",
    "delete_namespace",
    "ensure_schema",
    "is_configured",
    "list_profiles",
    "resolve_db_url",
    "upsert_profile",
]
