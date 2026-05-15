"""Shared test fixtures.

The ``engine`` fixture wires an ``Engine`` against a tmp permanent_store and
workdir — all tests should use this rather than touching the user's real
``permanent_store`` (default ``/Volumes/UNIVERSE_V/MEDIA/media_engine``).

Op-execution fixtures (``op_ctx``, sample media) arrive in commit 5 when the
first ops land.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from media_engine.config import EngineConfig
from media_engine.runtime.engine import Engine


@pytest.fixture
def engine_config(tmp_path: Path) -> EngineConfig:
    return EngineConfig(
        permanent_store=tmp_path / "store",
        workdir=tmp_path / "work",
        config_dir=tmp_path / "config",
        cache_db_url=f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
        log_format="text",
        log_level="WARNING",
    )


@pytest.fixture
def engine(engine_config: EngineConfig) -> Iterator[Engine]:
    with Engine.open_quick(engine_config) as e:
        yield e
