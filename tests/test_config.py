"""Config + storage validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_engine.config import EngineConfig


def test_default_config_loads() -> None:
    cfg = EngineConfig()
    assert cfg.namespace == "default"
    assert cfg.log_format == "text"
    assert cfg.min_free_gb == 20


def test_env_var_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "abc"))
    monkeypatch.setenv("MEDIA_ENGINE_LOG_FORMAT", "json")
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "5")
    cfg = EngineConfig()
    assert cfg.permanent_store == tmp_path / "abc"
    assert cfg.log_format == "json"
    assert cfg.min_free_gb == 5


def test_resolve_cache_db_url_default(tmp_path: Path) -> None:
    cfg = EngineConfig(permanent_store=tmp_path / "s")
    assert cfg.resolve_cache_db_url() == f"sqlite+pysqlite:///{tmp_path / 's' / 'cache.db'}"


def test_resolve_cache_db_url_override() -> None:
    cfg = EngineConfig(cache_db_url="postgresql://localhost/x")
    assert cfg.resolve_cache_db_url() == "postgresql://localhost/x"


def test_validate_storage_creates_missing_dir(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    cfg = EngineConfig(permanent_store=target)
    assert not target.exists()
    cfg.validate_storage()
    assert target.exists() and target.is_dir()


def test_validate_storage_fails_clearly_on_unwritable(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = EngineConfig(permanent_store=Path("/nonexistent_root_path/cannot_create"))
    with pytest.raises(RuntimeError, match="permanent_store"):
        cfg.validate_storage()
