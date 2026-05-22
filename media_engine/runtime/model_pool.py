"""Warm pool of in-process models.

Lazy-load expensive ML models (mlx-whisper, pyannote,
sentence-transformers) once per daemon session and reuse across ops.
Backends call ``model_pool.get_or_load(key, loader)`` and get back a
long-lived handle.

This module does NOT import any ML library — it's a generic registry. The
loader callable encapsulates the actual import and load.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Slot:
    instance: Any
    loaded_at_monotonic: float
    bytes_estimate: int = 0


@dataclass
class ModelPool:
    """Thread-safe lazy-load cache keyed by free-form string.

    The pool is intentionally small: no eviction, no LRU. Daemon restarts
    are the bulk-cleanup mechanism. If a backend wants to swap models,
    it calls ``forget(key)`` first.
    """

    _slots: dict[str, _Slot] = field(default_factory=lambda: {})
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get_or_load(
        self,
        key: str,
        loader: Callable[[], Any],
        *,
        bytes_estimate: int = 0,
    ) -> Any:
        with self._lock:
            slot = self._slots.get(key)
            if slot is not None:
                return slot.instance
        # Load outside the lock so concurrent loads of *different* keys don't
        # serialize. Same-key concurrent loads may double-load briefly; that's
        # acceptable for ML loaders that are de-facto idempotent.
        instance = loader()
        with self._lock:
            slot = self._slots.get(key)
            if slot is not None:
                # Another thread won the race; drop our load and return theirs.
                return slot.instance
            import time as _time
            self._slots[key] = _Slot(
                instance=instance,
                loaded_at_monotonic=_time.monotonic(),
                bytes_estimate=bytes_estimate,
            )
            return instance

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._slots

    def forget(self, key: str) -> bool:
        """Drop a model from the pool. Returns True if something was removed."""
        with self._lock:
            return self._slots.pop(key, None) is not None

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._slots.keys())

    def clear(self) -> None:
        with self._lock:
            self._slots.clear()

    def total_bytes_estimate(self) -> int:
        with self._lock:
            return sum(s.bytes_estimate for s in self._slots.values())
