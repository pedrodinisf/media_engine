"""Smoke test — package imports and version is set."""

import re

import media_engine


def test_version_is_set() -> None:
    # Assert format rather than pinning to a literal so the test doesn't
    # break every release cut. The strict pin caused a Phase-6.6 audit
    # miss when v0.6.2 shipped without updating this file.
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]+)?", media_engine.__version__), (
        f"version must be semver-like, got {media_engine.__version__!r}"
    )


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
