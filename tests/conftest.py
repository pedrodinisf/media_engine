"""Shared test fixtures.

The ``engine`` fixture wires an ``Engine`` against a tmp permanent_store and
workdir — all tests should use this rather than touching the user's real
``permanent_store`` (default ``/Volumes/UNIVERSE_V/MEDIA/media_engine``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from media_engine.config import EngineConfig
from media_engine.ops import OperationContext
from media_engine.runtime.engine import Engine

FIXTURE_DIR = Path(__file__).parent / "fixtures"


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


@pytest.fixture
def op_ctx(engine: Engine, tmp_path: Path) -> OperationContext:
    """Bare OperationContext suitable for op tests that don't need the DAG."""
    workdir = engine.storage.ensure_workdir("test-job")
    return OperationContext(
        workdir=workdir,
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
    )


@pytest.fixture
def sample_mp4() -> Path:
    p = FIXTURE_DIR / "sample.mp4"
    if not p.exists():
        pytest.skip("sample.mp4 missing — run `python tests/fixtures/build_fixtures.py`")
    return p


@pytest.fixture
def sample_m4a() -> Path:
    p = FIXTURE_DIR / "sample.m4a"
    if not p.exists():
        pytest.skip("sample.m4a missing — run `python tests/fixtures/build_fixtures.py`")
    return p


@pytest.fixture
def corrupt_mp4() -> Path:
    p = FIXTURE_DIR / "corrupt.mp4"
    if not p.exists():
        pytest.skip("corrupt.mp4 missing — run `python tests/fixtures/build_fixtures.py`")
    return p
