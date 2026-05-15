"""Shared test fixtures.

The ``engine`` fixture wires an ``Engine`` against a tmp permanent_store and
workdir — all tests should use this rather than touching the user's real
``permanent_store`` (default ``/Volumes/UNIVERSE_V/MEDIA/media_engine``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from media_engine.backends import BackendRegistry
from media_engine.config import EngineConfig
from media_engine.ops import OperationContext
from media_engine.runtime.engine import Engine

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _ensure_all_backends_registered() -> None:
    """Force-register every known backend.

    Module-import side effects only run once; tests that mutate the
    BackendRegistry (clear / unregister) leave it in an unknown state for
    the next test. This autouse fixture is idempotent — ``register`` is a
    no-op when the same class is already there.
    """
    # Trigger module imports so the backend classes exist.
    from media_engine.backends.sample_frames.ffmpeg_uniform import (
        FfmpegUniformBackend,
    )
    from media_engine.backends.transcribe.mlx_whisper import (
        MlxWhisperDetectLanguageBackend,
        MlxWhisperTranscribeBackend,
    )

    for backend_cls in (
        MlxWhisperTranscribeBackend,
        MlxWhisperDetectLanguageBackend,
        FfmpegUniformBackend,
    ):
        if not BackendRegistry.has(backend_cls.op_name, backend_cls.name):
            BackendRegistry.register(backend_cls)

    # pyannote is optional; only register when installed.
    try:
        from media_engine.backends.diarize.pyannote import PyannoteDiarizeBackend
    except ImportError:
        return
    if not BackendRegistry.has(
        PyannoteDiarizeBackend.op_name, PyannoteDiarizeBackend.name
    ):
        BackendRegistry.register(PyannoteDiarizeBackend)


def _ensure_all_ops_registered() -> None:
    """Same idea, for ``OpRegistry``."""
    # Importing the op modules registers via decorators on first import.
    from media_engine.ops.acquire import upload as _u  # noqa: F401
    from media_engine.ops.audio import (  # noqa: F401
        detect_language as _adl,
    )
    from media_engine.ops.frames import subsample as _fs  # noqa: F401
    from media_engine.ops.video import (  # noqa: F401
        extract_audio as _ve,
    )


@pytest.fixture(autouse=True)
def _ensure_registries() -> None:
    """Restore the full op + backend catalog before every test."""
    _ensure_all_ops_registered()
    _ensure_all_backends_registered()


@pytest.fixture
def engine_config(tmp_path: Path) -> EngineConfig:
    return EngineConfig(
        permanent_store=tmp_path / "store",
        workdir=tmp_path / "work",
        config_dir=tmp_path / "config",
        cache_db_url=f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
        log_format="text",
        log_level="WARNING",
        # Tests synthesize tiny fixtures; bypass the disk-space gate.
        min_free_gb=0,
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


@pytest.fixture
def sample_speech_wav() -> Path:
    p = FIXTURE_DIR / "sample_speech.wav"
    if not p.exists():
        pytest.skip(
            "sample_speech.wav missing — run `python tests/fixtures/build_fixtures.py` "
            "on macOS (requires `say`)"
        )
    return p


@pytest.fixture
def sample_dialogue_wav() -> Path:
    p = FIXTURE_DIR / "sample_dialogue.wav"
    if not p.exists():
        pytest.skip(
            "sample_dialogue.wav missing — run `python tests/fixtures/build_fixtures.py` "
            "on macOS (requires `say`)"
        )
    return p
