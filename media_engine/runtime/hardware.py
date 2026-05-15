"""Hardware capability checks for backend selection.

Ported pattern from framepulse ``local/hardware.py``: gate model loads on
available memory so we fail fast (with an actionable message) instead of
swap-thrashing.
"""

from __future__ import annotations

from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class MemoryFit:
    available_gb: float
    required_gb: float
    headroom_gb: float
    fits: bool


def total_memory_gb() -> float:
    return psutil.virtual_memory().total / (1024**3)


def available_memory_gb() -> float:
    return psutil.virtual_memory().available / (1024**3)


def check_model_fits(model_size_gb: float, headroom_gb: float = 4.0) -> MemoryFit:
    """Decide whether ``model_size_gb`` will fit in RAM with headroom.

    ``fits`` is True iff ``available - model_size >= headroom_gb``.
    """
    available = available_memory_gb()
    fits = (available - model_size_gb) >= headroom_gb
    return MemoryFit(
        available_gb=available,
        required_gb=model_size_gb,
        headroom_gb=headroom_gb,
        fits=fits,
    )


class HardwareCapacityError(RuntimeError):
    """Raised when ``check_model_fits`` reports the model won't fit."""


def assert_model_fits(model_size_gb: float, model_id: str = "<unnamed>",
                      headroom_gb: float = 4.0) -> None:
    fit = check_model_fits(model_size_gb, headroom_gb=headroom_gb)
    if not fit.fits:
        raise HardwareCapacityError(
            f"Model {model_id!r} ({model_size_gb:.1f} GB) won't fit: "
            f"only {fit.available_gb:.1f} GB available, need "
            f"{model_size_gb + headroom_gb:.1f} GB (incl. {headroom_gb:.0f} GB headroom)."
        )
