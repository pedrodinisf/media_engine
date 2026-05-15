"""Tests for runtime/hardware.py."""

from __future__ import annotations

import pytest

from media_engine.runtime.hardware import (
    HardwareCapacityError,
    assert_model_fits,
    available_memory_gb,
    check_model_fits,
    total_memory_gb,
)


def test_total_memory_gb_positive() -> None:
    assert total_memory_gb() > 0


def test_available_memory_gb_positive() -> None:
    assert available_memory_gb() > 0


def test_check_model_fits_zero_size_fits() -> None:
    fit = check_model_fits(0.0, headroom_gb=0.0)
    assert fit.fits is True
    assert fit.required_gb == 0.0


def test_check_model_fits_tiny_within_headroom() -> None:
    fit = check_model_fits(0.001, headroom_gb=0.001)
    assert fit.fits is True


def test_check_model_fits_huge_does_not_fit() -> None:
    fit = check_model_fits(1_000_000.0, headroom_gb=4.0)
    assert fit.fits is False


def test_assert_model_fits_passes_when_room() -> None:
    assert_model_fits(0.001, model_id="tiny-test", headroom_gb=0.001)


def test_assert_model_fits_raises_with_actionable_message() -> None:
    with pytest.raises(HardwareCapacityError, match=r"won't fit"):
        assert_model_fits(1_000_000.0, model_id="impossible", headroom_gb=4.0)
