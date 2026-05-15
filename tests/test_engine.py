"""Tests for runtime/engine.py — Phase 0 read-only surface + lineage."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from media_engine.artifacts import Audio, Kind, Video
from media_engine.config import EngineConfig
from media_engine.runtime.engine import Engine


def _now() -> datetime:
    return datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


def test_open_quick_validates_storage(tmp_path: Path) -> None:
    cfg = EngineConfig(
        permanent_store=tmp_path / "fresh_store",
        workdir=tmp_path / "work",
        cache_db_url=f"sqlite+pysqlite:///{tmp_path / 'c.db'}",
    )
    with Engine.open_quick(cfg) as e:
        assert (tmp_path / "fresh_store").exists()
        assert e.config is cfg


def test_engine_get_artifact_returns_none_for_missing(engine: Engine) -> None:
    assert engine.get_artifact("nope") is None


def test_engine_round_trip_artifact(engine: Engine, tmp_path: Path) -> None:
    v = Video(id="vid12345", path=tmp_path / "x.mp4", created_at=_now())
    engine.cache.upsert_artifact(v)
    back = engine.get_artifact("vid12345")
    assert back == v


def test_engine_list_artifacts_kind_filter(engine: Engine, tmp_path: Path) -> None:
    engine.cache.upsert_artifact(
        Video(id="vid12345", path=tmp_path / "v.mp4", created_at=_now())
    )
    engine.cache.upsert_artifact(
        Audio(
            id="aud12345",
            path=tmp_path / "a.wav",
            derived_from=("vid12345",),
            created_at=_now(),
        )
    )
    audios = engine.list_artifacts(kind=Kind.Audio)
    assert {a.id for a in audios} == {"aud12345"}


def test_engine_resolve_id_unique(engine: Engine, tmp_path: Path) -> None:
    engine.cache.upsert_artifact(
        Video(id="abcd1234defg", path=tmp_path / "v.mp4", created_at=_now())
    )
    assert engine.resolve_id("abcd") == "abcd1234defg"


def test_engine_resolve_id_ambiguous_raises(engine: Engine, tmp_path: Path) -> None:
    engine.cache.upsert_artifact(
        Video(id="abcd1111", path=tmp_path / "v1.mp4", created_at=_now())
    )
    engine.cache.upsert_artifact(
        Video(id="abcd2222", path=tmp_path / "v2.mp4", created_at=_now())
    )
    with pytest.raises(LookupError, match="Ambiguous"):
        engine.resolve_id("abcd")


def test_engine_resolve_id_miss_raises(engine: Engine) -> None:
    with pytest.raises(LookupError, match="No artifact"):
        engine.resolve_id("zzzz")


def test_engine_lineage_returns_node(engine: Engine, tmp_path: Path) -> None:
    engine.cache.upsert_artifact(
        Video(id="vid000", path=tmp_path / "v.mp4", created_at=_now())
    )
    engine.cache.upsert_artifact(
        Audio(
            id="aud000",
            path=tmp_path / "a.wav",
            derived_from=("vid000",),
            created_at=_now(),
        )
    )
    tree = engine.lineage("aud000")
    assert tree is not None
    assert tree.artifact.id == "aud000"
    assert len(tree.parents) == 1
    assert tree.parents[0].artifact.id == "vid000"


def test_engine_context_manager_closes_cache(engine_config: EngineConfig) -> None:
    with Engine.open_quick(engine_config) as e:
        assert e.cache.engine is not None
    # after context exit, calling close again should be safe (idempotent)
    e.close()


def test_engine_open_session_alias(engine_config: EngineConfig) -> None:
    """Phase 0: open_session == open_quick. Phase 1 makes it heavyweight."""
    with Engine.open_session(engine_config) as e:
        assert e.config.namespace == "default"
