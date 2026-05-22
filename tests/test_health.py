"""Health + readiness — both the runtime probe and the REST endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from media_engine.api.app import build_app
from media_engine.cli import app
from media_engine.config import EngineConfig
from media_engine.runtime.engine import Engine
from media_engine.runtime.health import liveness, readiness


def test_liveness_is_always_ok() -> None:
    report = liveness()
    assert report.alive
    assert report.ready


def test_readiness_passes_against_tmp_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
    )
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")
    report = readiness()
    assert report.ready
    names = {c.name for c in report.checks}
    assert "permanent_store" in names
    assert "cache_db" in names


def test_readiness_reports_unreachable_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bogus Postgres URL → cache check goes down → ready=False."""
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        "postgresql+psycopg://nonexistent:1@127.0.0.1:1/none",
    )
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")
    report = readiness()
    assert not report.ready
    cache_check = next(c for c in report.checks if c.name == "cache_db")
    assert cache_check.status == "down"


def test_readiness_write_probe_detects_readonly_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.access(W_OK)`` lies on a read-only mount; the real probe
    is writing + deleting a file. Simulate a read-only store by
    monkeypatching the write call inside ``_check_storage_writable``."""
    from media_engine.runtime import health as _h

    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(store))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
    )
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")

    real_write_bytes = Path.write_bytes

    def _fail_for_probe(self: Path, data: bytes) -> int:
        if ".health-probe-" in self.name:
            raise PermissionError("simulated read-only mount")
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", _fail_for_probe)
    report = _h.readiness()
    perm = next(c for c in report.checks if c.name == "permanent_store")
    assert perm.status == "down"
    assert not report.ready


def test_readiness_free_space_check_reports_down_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When free space drops below ``min_free_gb`` the probe must
    report ``down`` so kubelet pulls the pod out of traffic before
    the engine's disk-guard error starts surfacing to clients."""
    from media_engine.runtime import health as _h

    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(store))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
    )
    # Wildly impossible threshold so we always trip "down".
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "999999")
    report = _h.readiness()
    fs_check = next(c for c in report.checks if c.name == "free_space")
    assert fs_check.status == "down"
    assert not report.ready


# ─────────────────────────────────────────────────────────────────
# REST
# ─────────────────────────────────────────────────────────────────


def test_health_endpoint_unauthenticated(tmp_path: Path) -> None:
    cfg = EngineConfig(
        permanent_store=tmp_path / "store",
        workdir=tmp_path / "work",
        config_dir=tmp_path / "config",
        cache_db_url=f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
        min_free_gb=0,
    )
    with Engine.open_quick(cfg) as e:
        app_ = build_app(engine=e)
        with TestClient(app_) as c:
            r = c.get("/health")
            assert r.status_code == 200
            body = r.json()
            assert body["alive"] is True


def test_ready_endpoint_unauthenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /ready endpoint must respond without a token."""
    # Point env at the same tmp store so readiness() observes a healthy
    # cache file when it runs server-side.
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
    )
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")
    cfg = EngineConfig.load()
    with Engine.open_quick(cfg) as e:
        app_ = build_app(engine=e)
        with TestClient(app_) as c:
            r = c.get("/ready")
            assert r.status_code in {200, 503}  # either is a structured response
            body = r.json()
            assert "checks" in body


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────


def test_cli_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
    )
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")
    runner = CliRunner()
    r = runner.invoke(app, ["health", "--json"])
    assert r.exit_code == 0
    body = __import__("json").loads(r.stdout)
    assert body["alive"] is True


def test_cli_ready_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
    )
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")
    runner = CliRunner()
    r = runner.invoke(app, ["ready"])
    assert r.exit_code == 0
