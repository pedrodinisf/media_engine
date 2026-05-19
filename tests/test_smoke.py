"""Smoke test — package imports and version is set."""

import media_engine


def test_version_is_set() -> None:
    assert media_engine.__version__ == "0.1.0"


def test_package_imports() -> None:
    assert media_engine is not None


def test_public_api_reexports() -> None:
    """Plan §4: the supported import surface must be importable from the
    package root."""
    from media_engine import (
        Artifact,
        Engine,
        Kind,
        Pipeline,
        register_backend,
        register_op,
    )

    assert all(
        s is not None
        for s in (Engine, Pipeline, Artifact, Kind, register_op, register_backend)
    )
    assert set(media_engine.__all__) >= {
        "Engine", "Pipeline", "Artifact", "Kind",
        "register_op", "register_backend",
    }
