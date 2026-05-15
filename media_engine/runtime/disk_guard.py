"""Disk-space gate.

Cheap precondition for ``Engine.run``: refuse to start an op when the
permanent_store filesystem has less than ``MEDIA_ENGINE_MIN_FREE_GB``
(default 20 GB) free. Catches the ``acquire 50 GB livestream onto a 30 GB
volume`` class of failure before it starts writing.
"""

from __future__ import annotations

import shutil
from pathlib import Path


class InsufficientDiskSpaceError(RuntimeError):
    """Raised when the storage volume's free space falls below the threshold."""


def free_gb(path: Path) -> float:
    """Free disk space (in GB) on the filesystem containing ``path``."""
    target = path if path.exists() else path.parent
    while not target.exists() and target != target.parent:
        target = target.parent
    usage = shutil.disk_usage(target)
    return usage.free / (1024**3)


def assert_free_space(path: Path, min_gb: float) -> None:
    """Raise ``InsufficientDiskSpaceError`` when free < ``min_gb`` GB."""
    actual = free_gb(path)
    if actual < min_gb:
        raise InsufficientDiskSpaceError(
            f"Refusing op: {path} has {actual:.1f} GB free, "
            f"below MEDIA_ENGINE_MIN_FREE_GB={min_gb}. "
            f"Free up space or lower the threshold."
        )
