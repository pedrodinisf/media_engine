"""``med db`` — alembic migrations + sqlite-to-postgres copy.

The migration test runs against a temp SQLite path (no Postgres needed).
The sqlite-to-postgres test is gated by ``MEDIA_ENGINE_TEST_POSTGRES_URL``
— without it we still cover the digest function on a SQLite database
to prove the determinism invariant.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from media_engine.artifacts import Kind
from media_engine.artifacts.text import MarkdownArtifact
from media_engine.cli import app
from media_engine.cli.db import _digest


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv("MEDIA_ENGINE_CACHE_DB_URL", f"sqlite+pysqlite:///{tmp_path / 'cache.db'}")
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_db_migrate_brings_up_schema(
    runner: CliRunner, cli_env: Path
) -> None:
    """``med db migrate`` on a fresh path creates the full schema."""
    result = runner.invoke(app, ["db", "migrate"])
    assert result.exit_code == 0, result.stdout
    # Tables should now exist; verify via SQLAlchemy inspection.
    from sqlalchemy import inspect

    engine = create_engine(
        f"sqlite+pysqlite:///{cli_env / 'cache.db'}", future=True
    )
    tables = set(inspect(engine).get_table_names())
    assert "cached_artifacts" in tables
    assert "cached_operation_runs" in tables
    assert "cost_log" in tables
    assert "events" in tables
    assert "jobs" in tables
    assert "api_tokens" in tables


def test_db_migrate_is_idempotent(runner: CliRunner, cli_env: Path) -> None:
    r1 = runner.invoke(app, ["db", "migrate"])
    r2 = runner.invoke(app, ["db", "migrate"])
    assert r1.exit_code == 0
    assert r2.exit_code == 0


def test_db_migrate_respects_db_url_override(
    runner: CliRunner, cli_env: Path, tmp_path: Path
) -> None:
    """``med db migrate --db-url X`` must use X even when the env says Y.

    Regression: env.py used to unconditionally re-resolve the URL from
    ``EngineConfig.load()``, silently shadowing the CLI argument.
    cli/db.py now stamps ``url_source='cli'`` on the alembic config
    and env.py honors it.
    """
    explicit = tmp_path / "explicit.db"
    result = runner.invoke(
        app,
        [
            "db",
            "migrate",
            "--db-url",
            f"sqlite+pysqlite:///{explicit}",
        ],
    )
    assert result.exit_code == 0, result.stdout
    # The cache.db in the env (cli_env) should NOT have been written.
    assert explicit.exists()
    env_default = cli_env / "cache.db"
    assert not env_default.exists() or env_default.stat().st_size == 0


def test_alembic_dir_lives_in_package() -> None:
    """The migrations must ship inside ``media_engine`` so the wheel
    install carries them.

    Pre-Phase-4 we kept ``alembic/`` at the repo root, but the wheel
    config only packages ``media_engine/`` — so an installed user
    couldn't run ``med db migrate``. Phase 4 moves the files into
    ``media_engine/_alembic/``. This regression test makes sure the
    move isn't reverted by accident.
    """
    import media_engine
    from media_engine.cli.db import _alembic_config

    package_root = Path(media_engine.__file__).resolve().parent
    alembic_dir = package_root / "_alembic"
    assert alembic_dir.is_dir()
    assert (alembic_dir / "env.py").is_file()
    assert (alembic_dir / "versions").is_dir()

    cfg = _alembic_config("sqlite+pysqlite:///./nope.db")
    assert cfg.get_main_option("script_location") == str(alembic_dir)


def test_digest_is_deterministic_under_reorder(tmp_path: Path) -> None:
    """Insert rows in two different orders into two SQLite files; the
    digest must agree because we sort by primary key before hashing."""
    from media_engine.runtime.cache import Cache

    cache_a = Cache(f"sqlite+pysqlite:///{tmp_path / 'a.db'}")
    cache_b = Cache(f"sqlite+pysqlite:///{tmp_path / 'b.db'}")

    arts = []
    for i in range(3):
        art = MarkdownArtifact(
            id=f"{i:064x}",
            kind=Kind.MarkdownArtifact,
            path=str(tmp_path / f"{i}.md"),  # type: ignore[arg-type]
            metadata={"i": i},
            derived_from=(),
            produced_by=None,
            created_at=datetime.now(UTC),
        )
        arts.append(art)

    for art in arts:
        cache_a.upsert_artifact(art)
    for art in reversed(arts):
        cache_b.upsert_artifact(art)

    factory_a = sessionmaker(bind=cache_a.engine, expire_on_commit=False)
    factory_b = sessionmaker(bind=cache_b.engine, expire_on_commit=False)
    assert _digest(factory_a) == _digest(factory_b)


@pytest.mark.needs_postgres
def test_dump_sqlite_to_postgres_round_trip(
    runner: CliRunner, cli_env: Path, tmp_path: Path
) -> None:
    """End-to-end: write a few rows to SQLite, copy to Postgres, expect
    matching digests + the destination tables to be populated."""
    pg_url = os.environ.get("MEDIA_ENGINE_TEST_POSTGRES_URL")
    if not pg_url:
        pytest.skip("MEDIA_ENGINE_TEST_POSTGRES_URL not set")
    from media_engine.runtime.cache import Cache

    src_url = f"sqlite+pysqlite:///{cli_env / 'cache.db'}"
    cache = Cache(src_url)
    art = MarkdownArtifact(
        id="d" * 64,
        kind=Kind.MarkdownArtifact,
        path=str(tmp_path / "d.md"),  # type: ignore[arg-type]
        metadata={"hello": "world"},
        derived_from=(),
        produced_by=None,
        created_at=datetime.now(UTC),
    )
    cache.upsert_artifact(art)
    cache.close()

    result = runner.invoke(
        app,
        [
            "db",
            "dump-sqlite-to-postgres",
            "--from",
            src_url,
            "--to",
            pg_url,
        ],
    )
    assert result.exit_code == 0, result.stdout
    # The destination should now have the same artifact.
    dst_cache = Cache(pg_url)
    try:
        got = dst_cache.get_artifact(art.id)
        assert got is not None
        assert got.metadata == {"hello": "world"}
    finally:
        dst_cache.close()
