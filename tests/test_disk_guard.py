"""Tests for runtime/disk_guard.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_engine.runtime.disk_guard import (
    InsufficientDiskSpaceError,
    assert_free_space,
    free_gb,
)


def test_free_gb_returns_positive_for_existing_path(tmp_path: Path) -> None:
    assert free_gb(tmp_path) > 0


def test_free_gb_walks_up_to_existing_ancestor(tmp_path: Path) -> None:
    nonexistent = tmp_path / "nope" / "still_nope" / "leaf"
    assert free_gb(nonexistent) > 0


def test_assert_free_space_passes_with_zero_threshold(tmp_path: Path) -> None:
    assert_free_space(tmp_path, min_gb=0.0)


def test_assert_free_space_passes_below_actual(tmp_path: Path) -> None:
    actual = free_gb(tmp_path)
    # Use half of actual to guarantee pass.
    assert_free_space(tmp_path, min_gb=actual / 2)


def test_assert_free_space_raises_above_actual(tmp_path: Path) -> None:
    actual = free_gb(tmp_path)
    with pytest.raises(InsufficientDiskSpaceError, match="below"):
        assert_free_space(tmp_path, min_gb=actual + 1_000_000)
