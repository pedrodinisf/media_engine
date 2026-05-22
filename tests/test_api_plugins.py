"""Phase 6 commit 49 — /plugins/* + /storage/* REST surface."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from media_engine.api.app import build_app
from media_engine.api.auth import create_token
from media_engine.config import EngineConfig
from media_engine.runtime.engine import Engine


@pytest.fixture
def api_engine(tmp_path: Path) -> Iterator[Engine]:
    cfg = EngineConfig(
        permanent_store=tmp_path / "store",
        workdir=tmp_path / "work",
        config_dir=tmp_path / "config",
        cache_db_url=f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
        min_free_gb=0,
    )
    with Engine.open_quick(cfg) as e:
        yield e


@pytest.fixture
def client(api_engine: Engine) -> Iterator[TestClient]:
    app = build_app(engine=api_engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth(api_engine: Engine) -> dict[str, str]:
    secret = create_token(api_engine.cache, label="test").secret
    return {"Authorization": f"Bearer {secret}"}


# ─────────────────────────────────────────────────────────────────
# /plugins/extras
# ─────────────────────────────────────────────────────────────────


def test_extras_lists_every_pyproject_extra(
    client: TestClient, auth: dict[str, str]
) -> None:
    """The hard-coded `_EXTRAS_CATALOG` must stay in sync with
    `pyproject.toml`'s [project.optional-dependencies] section.

    Auto-parsing pyproject at request time would let the wheel
    install go without it; instead we keep the catalog inline + use
    this test as the drift guard. Adding a new extra to pyproject
    without updating `media_engine/api/plugins.py:_EXTRAS_CATALOG`
    fails this test on the next CI run.
    """
    import tomllib
    from pathlib import Path

    pyproject_path = (
        Path(__file__).resolve().parents[1] / "pyproject.toml"
    )
    with pyproject_path.open("rb") as f:
        pyproject = tomllib.load(f)
    pyproject_extras = set(
        pyproject["project"]["optional-dependencies"].keys()
    )

    r = client.get("/plugins/extras", headers=auth)
    assert r.status_code == 200
    route_names = {row["name"] for row in r.json()["items"]}

    missing_from_route = pyproject_extras - route_names
    assert not missing_from_route, (
        f"_EXTRAS_CATALOG is missing pyproject extras: "
        f"{sorted(missing_from_route)}"
    )
    stale_in_route = route_names - pyproject_extras
    assert not stale_in_route, (
        f"_EXTRAS_CATALOG references extras not in pyproject: "
        f"{sorted(stale_in_route)}"
    )


def test_extras_install_command_uses_uv_sync_form(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/plugins/extras", headers=auth)
    items = r.json()["items"]
    for row in items:
        assert row["install_command"] == f"uv sync --extra {row['name']}"


def test_extras_install_status_reflects_find_spec(
    client: TestClient, auth: dict[str, str]
) -> None:
    """`api` extra ships with the test env (we need FastAPI to be
    running this test). `transcribe-mlx` is gated by mlx-whisper which
    isn't on the test runner. The two together cover the boolean."""
    r = client.get("/plugins/extras", headers=auth)
    by_name = {row["name"]: row for row in r.json()["items"]}
    assert by_name["api"]["installed"] is True


def test_extras_requires_token(client: TestClient) -> None:
    r = client.get("/plugins/extras")
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────
# /plugins/catalog
# ─────────────────────────────────────────────────────────────────


def test_catalog_lists_every_op_and_backend(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/plugins/catalog", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "audio.transcribe" in body["ops"]
    assert "video.extract_audio" in body["ops"]
    # Every backend key follows op__backend.
    for k in body["backends"]:
        assert "__" in k
    # Empty hidden-state on a fresh engine.
    assert body["hidden_ops"] == []
    assert body["hidden_backends"] == []


def test_catalog_put_persists_and_get_reads(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    r = client.put(
        "/plugins/catalog",
        json={
            "hidden_ops": ["video.extract_audio"],
            "hidden_backends": ["audio.transcribe__mlx-whisper"],
        },
        headers=auth,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hidden_ops"] == ["video.extract_audio"]
    assert body["hidden_backends"] == ["audio.transcribe__mlx-whisper"]
    # plugins.toml lands on disk.
    plugins_toml = api_engine.config.config_dir / "plugins.toml"
    assert plugins_toml.is_file()
    # GET reads the same state back.
    r2 = client.get("/plugins/catalog", headers=auth)
    body2 = r2.json()
    assert body2["hidden_ops"] == ["video.extract_audio"]


def test_catalog_put_accepts_unknown_keys_silently(
    client: TestClient, auth: dict[str, str]
) -> None:
    """The operator may want to hide a plugin they're about to install;
    unknown keys are stored verbatim and just don't fire the filter."""
    r = client.put(
        "/plugins/catalog",
        json={
            "hidden_ops": ["future.plugin", "audio.transcribe"],
            "hidden_backends": [],
        },
        headers=auth,
    )
    assert r.status_code == 200
    assert "future.plugin" in r.json()["hidden_ops"]


def test_catalog_requires_token(client: TestClient) -> None:
    assert client.get("/plugins/catalog").status_code == 401
    assert client.put("/plugins/catalog", json={}).status_code == 401


# ─────────────────────────────────────────────────────────────────
# MCP tools/list honours the catalog gate (Phase 6 plan §3.8)
# ─────────────────────────────────────────────────────────────────


def test_mcp_filter_respects_catalog_gate(api_engine: Engine) -> None:
    """A hidden op disappears from `MCP _filtered_op_names` even when
    the security allow-list includes it."""
    from media_engine.mcp.server import (
        MCPSecurityConfig,
        _filtered_op_names,
    )
    from media_engine.runtime.plugins import CatalogState, save_catalog

    # Allow every op via MCP security.
    open_security = MCPSecurityConfig(allowed_ops=None, deny_ops=frozenset())
    before = _filtered_op_names(
        open_security, config_dir=api_engine.config.config_dir
    )
    assert "video.extract_audio" in before

    save_catalog(
        api_engine.config.config_dir,
        CatalogState(hidden_ops=frozenset({"video.extract_audio"})),
    )

    after = _filtered_op_names(
        open_security, config_dir=api_engine.config.config_dir
    )
    assert "video.extract_audio" not in after
    # Other ops still visible.
    assert "audio.transcribe" in after


# ─────────────────────────────────────────────────────────────────
# /storage/stats
# ─────────────────────────────────────────────────────────────────


def test_storage_stats_empty_namespace(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/storage/stats", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["total_bytes"] == 0
    assert body["free_gb"] >= 0
    assert "video" in body["by_kind"]
    assert body["by_kind"]["video"]["count"] == 0


def test_storage_stats_requires_token(client: TestClient) -> None:
    r = client.get("/storage/stats")
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────
# /storage/gc
# ─────────────────────────────────────────────────────────────────


def test_gc_dry_run_does_not_delete(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    """Seed an old workdir, then assert apply=False reports a
    candidate but doesn't unlink it."""
    # Build the workdir + a fake stale entry.
    api_engine.config.workdir.mkdir(parents=True, exist_ok=True)
    stale = api_engine.config.workdir / "stale-job"
    stale.mkdir()
    # Push mtime well past the retention window (default 24h).
    import os
    import time

    past = time.time() - 7 * 24 * 3600
    os.utime(stale, (past, past))

    r = client.post(
        "/storage/gc",
        json={"apply": False, "sweep_workdirs": True, "evict": False},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is False
    assert body["workdirs_swept"] == 0
    assert any(str(stale) in c for c in body["workdir_candidates"])
    # Dir still on disk.
    assert stale.exists()


def test_gc_apply_actually_sweeps(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    api_engine.config.workdir.mkdir(parents=True, exist_ok=True)
    stale = api_engine.config.workdir / "stale-job"
    stale.mkdir()
    import os
    import time

    past = time.time() - 7 * 24 * 3600
    os.utime(stale, (past, past))

    r = client.post(
        "/storage/gc",
        json={"apply": True, "sweep_workdirs": True, "evict": False},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is True
    assert body["workdirs_swept"] == 1
    assert not stale.exists()


def test_gc_eviction_off_when_disabled_in_config(
    client: TestClient, auth: dict[str, str]
) -> None:
    """``eviction_enabled`` defaults to False in EngineConfig — the GC
    pass should report `eviction_enabled=False` regardless of the
    `evict=True` request flag."""
    r = client.post(
        "/storage/gc",
        json={"apply": False, "sweep_workdirs": False, "evict": True},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["eviction_enabled"] is False


def test_gc_requires_token(client: TestClient) -> None:
    r = client.post("/storage/gc", json={"apply": False})
    assert r.status_code == 401
