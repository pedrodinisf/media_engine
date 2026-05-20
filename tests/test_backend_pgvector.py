"""``pgvector`` + ``postgres-tsvector`` — registration + safety checks.

Live Postgres integration runs only when ``MEDIA_ENGINE_TEST_POSTGRES_URL``
is set; on every machine we still verify the modules import clean,
register through bootstrap, and refuse to operate against a SQLite
cache URL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from media_engine.backends import BackendRegistry


def test_pgvector_backend_registered() -> None:
    assert BackendRegistry.has("search.semantic", "pgvector")
    backend_cls = BackendRegistry.get("search.semantic", "pgvector")
    assert backend_cls.name == "pgvector"
    assert "postgres" in backend_cls.requires.services


def test_postgres_tsvector_backend_registered() -> None:
    assert BackendRegistry.has("search.fulltext", "postgres-tsvector")
    backend_cls = BackendRegistry.get("search.fulltext", "postgres-tsvector")
    assert backend_cls.name == "postgres-tsvector"


def test_pgvector_refuses_sqlite_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing the env at a SQLite URL must be rejected at resolve time
    (not silently used) — pgvector can't read SQLite vector data."""
    monkeypatch.setenv(
        "MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store")
    )
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
    )
    monkeypatch.delenv("MEDIA_ENGINE_SEMANTIC_DB_URL", raising=False)
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")

    from media_engine.backends.search.pgvector import _resolve_db_url

    with pytest.raises(RuntimeError, match="SQLite"):
        _resolve_db_url()


def test_postgres_tsvector_refuses_sqlite_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store")
    )
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
    )
    monkeypatch.delenv("MEDIA_ENGINE_FULLTEXT_DB_URL", raising=False)

    from media_engine.backends.search.postgres_tsvector import (
        _resolve_db_url,
    )

    with pytest.raises(RuntimeError, match="SQLite"):
        _resolve_db_url()
