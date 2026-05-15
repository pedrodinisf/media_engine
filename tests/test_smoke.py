"""Smoke test — package imports and version is set."""

import media_engine


def test_version_is_set() -> None:
    assert media_engine.__version__ == "0.1.0"


def test_package_imports() -> None:
    assert media_engine is not None
