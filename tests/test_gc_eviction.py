"""``runtime/gc`` + ``runtime/eviction`` + ``med storage`` tests."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from media_engine.artifacts import Kind
from media_engine.artifacts.text import MarkdownArtifact
from media_engine.cli import app
from media_engine.config import EngineConfig
from media_engine.runtime.cache import Cache
from media_engine.runtime.eviction import EvictionPolicy, evict_lru
from media_engine.runtime.gc import gc_interval_from_env, sweep_workdirs


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
    )
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ─────────────────────────────────────────────────────────────────
# Workdir GC
# ─────────────────────────────────────────────────────────────────


def test_sweep_workdirs_drops_old_dirs(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    old = workdir / "old-job"
    new = workdir / "new-job"
    old.mkdir()
    new.mkdir()
    # Backdate the old one.
    cutoff = time.time() - timedelta(hours=48).total_seconds()
    os.utime(old, (cutoff, cutoff))

    removed = sweep_workdirs(workdir, retention=timedelta(hours=24))
    removed_names = {p.name for p in removed}
    assert "old-job" in removed_names
    assert "new-job" not in removed_names
    assert not old.exists()
    assert new.exists()


def test_sweep_workdirs_missing_directory_is_noop(tmp_path: Path) -> None:
    assert sweep_workdirs(tmp_path / "does-not-exist") == []


def test_gc_interval_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDIA_ENGINE_GC_INTERVAL", raising=False)
    assert gc_interval_from_env(default_seconds=42) == 42
    monkeypatch.setenv("MEDIA_ENGINE_GC_INTERVAL", "120")
    assert gc_interval_from_env() == 120
    monkeypatch.setenv("MEDIA_ENGINE_GC_INTERVAL", "not-a-number")
    assert gc_interval_from_env(default_seconds=99) == 99


# ─────────────────────────────────────────────────────────────────
# Eviction
# ─────────────────────────────────────────────────────────────────


def _plant_artifact(
    cache: Cache, tmp_path: Path, kind: Kind, age_hours: float, size_bytes: int
) -> str:
    """Materialize a markdown-like artifact + its file on disk."""
    aid = f"{kind.value}-{age_hours}-{size_bytes}".ljust(64, "0")
    path = tmp_path / f"{aid}.bin"
    path.write_bytes(b"\x00" * size_bytes)
    cache.upsert_artifact(
        MarkdownArtifact(
            id=aid,
            kind=kind,
            path=str(path),  # type: ignore[arg-type]
            metadata={},
            derived_from=(),
            produced_by=None,
            created_at=datetime.now(UTC) - timedelta(hours=age_hours),
        )
    )
    return aid


def test_eviction_disabled_returns_zero(tmp_path: Path) -> None:
    cache = Cache(f"sqlite+pysqlite:///{tmp_path / 'c.db'}")
    res = evict_lru(cache, EvictionPolicy(enabled=False))
    assert res.evicted_ids == []
    assert res.freed_bytes == 0


def test_eviction_under_cap_does_nothing(tmp_path: Path) -> None:
    cache = Cache(f"sqlite+pysqlite:///{tmp_path / 'c.db'}")
    _plant_artifact(cache, tmp_path, Kind.MarkdownArtifact, 24, 10)
    policy = EvictionPolicy(enabled=True, max_gb=1.0)
    res = evict_lru(cache, policy)
    assert res.evicted_ids == []
    assert res.bytes_before == res.bytes_after


def test_eviction_removes_oldest_non_protected(tmp_path: Path) -> None:
    cache = Cache(f"sqlite+pysqlite:///{tmp_path / 'c.db'}")
    # Three markdown artifacts, ages 10/20/30 hours. Cap is tight so two
    # need to be evicted; the oldest non-protected ones go first.
    a_new = _plant_artifact(cache, tmp_path, Kind.MarkdownArtifact, 1, 500)
    a_mid = _plant_artifact(cache, tmp_path, Kind.MarkdownArtifact, 12, 500)
    a_old = _plant_artifact(cache, tmp_path, Kind.MarkdownArtifact, 48, 500)

    cap_bytes = 600  # only one markdown should fit
    policy = EvictionPolicy(
        enabled=True,
        max_gb=cap_bytes / (1024**3),
        protected_kinds=(Kind.Video, Kind.Audio),
    )
    res = evict_lru(cache, policy)
    # Oldest (a_old) and middle (a_mid) get evicted; newest stays.
    assert a_old in res.evicted_ids
    assert a_mid in res.evicted_ids
    assert a_new not in res.evicted_ids
    assert res.bytes_after <= cap_bytes


def test_eviction_skips_protected_kinds(tmp_path: Path) -> None:
    """Protected kinds (Video/Audio default) survive even if oldest."""
    # We can't directly create a Video without exhaustive metadata, so
    # protect MarkdownArtifact instead and verify the same code path.
    cache = Cache(f"sqlite+pysqlite:///{tmp_path / 'c.db'}")
    aid = _plant_artifact(cache, tmp_path, Kind.MarkdownArtifact, 100, 5000)
    policy = EvictionPolicy(
        enabled=True,
        max_gb=0.0,
        protected_kinds=(Kind.MarkdownArtifact,),
    )
    res = evict_lru(cache, policy)
    assert res.evicted_ids == []
    assert aid in {a.id for a in cache.list_artifacts(limit=10)}


def test_eviction_dry_run_does_not_delete(tmp_path: Path) -> None:
    cache = Cache(f"sqlite+pysqlite:///{tmp_path / 'c.db'}")
    aid = _plant_artifact(cache, tmp_path, Kind.MarkdownArtifact, 48, 1024)
    policy = EvictionPolicy(enabled=True, max_gb=0.0)
    res = evict_lru(cache, policy, dry_run=True)
    assert aid in res.evicted_ids
    # File + row should still be there.
    assert cache.get_artifact(aid) is not None


# ─────────────────────────────────────────────────────────────────
# CLI surface
# ─────────────────────────────────────────────────────────────────


def test_storage_stats_runs(runner: CliRunner, cli_env: Path) -> None:
    res = runner.invoke(app, ["storage", "stats", "--json"])
    assert res.exit_code == 0, res.stdout
    import json as _json

    payload = _json.loads(res.stdout)
    assert "permanent_store" in payload
    assert "per_kind" in payload


def test_storage_gc_dry_run(runner: CliRunner, cli_env: Path) -> None:
    res = runner.invoke(app, ["storage", "gc"])
    assert res.exit_code == 0, res.stdout
    assert "workdir sweep" in res.stdout.lower()


def test_storage_migrate_rewrites_paths(
    runner: CliRunner, cli_env: Path
) -> None:
    """med storage migrate rewrites the path prefix of every cache row."""
    cache = Cache(f"sqlite+pysqlite:///{cli_env / 'cache.db'}")
    old_root = cli_env / "old"
    new_root = cli_env / "new"
    old_root.mkdir()
    new_root.mkdir()
    src = old_root / "a.bin"
    src.write_bytes(b"x")
    cache.upsert_artifact(
        MarkdownArtifact(
            id="m" * 64,
            kind=Kind.MarkdownArtifact,
            path=str(src),  # type: ignore[arg-type]
            metadata={},
            derived_from=(),
            produced_by=None,
            created_at=datetime.now(UTC),
        )
    )
    cache.close()
    res = runner.invoke(
        app,
        [
            "storage",
            "migrate",
            "--from",
            str(old_root),
            "--to",
            str(new_root),
        ],
    )
    assert res.exit_code == 0, res.stdout
    cache = Cache(f"sqlite+pysqlite:///{cli_env / 'cache.db'}")
    try:
        art = cache.get_artifact("m" * 64)
        assert art is not None
        assert str(art.path).startswith(str(new_root))
    finally:
        cache.close()


def test_namespace_flag_isolates_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artifact registered in namespace A must not be visible from B."""
    cfg = EngineConfig(
        permanent_store=tmp_path / "store",
        workdir=tmp_path / "work",
        config_dir=tmp_path / "config",
        cache_db_url=f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
        min_free_gb=0,
        namespace="ns-a",
    )
    cache = Cache(cfg.resolve_cache_db_url())
    try:
        aid = "ns" * 32  # 64 chars
        path = tmp_path / "ns.bin"
        path.write_bytes(b"x")
        cache.upsert_artifact(
            MarkdownArtifact(
                id=aid,
                kind=Kind.MarkdownArtifact,
                path=str(path),  # type: ignore[arg-type]
                metadata={},
                derived_from=(),
                produced_by=None,
                namespace="ns-a",
                created_at=datetime.now(UTC),
            )
        )
        assert cache.get_artifact(aid, namespace="ns-a") is not None
        assert cache.get_artifact(aid, namespace="ns-b") is None
        # list_artifacts respects namespace too.
        from_a = cache.list_artifacts(namespace="ns-a")
        from_b = cache.list_artifacts(namespace="ns-b")
        assert any(a.id == aid for a in from_a)
        assert not any(a.id == aid for a in from_b)
    finally:
        cache.close()
