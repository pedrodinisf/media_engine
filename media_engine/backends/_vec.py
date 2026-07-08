"""Shared vector helpers for the brute-force SQLite vector stores.

Extracted from ``backends/search/sqlite.py`` so ``search.semantic`` and the
Phase-7 ``speakers.match`` / ``speakers.cluster`` fingerprint store share one
cosine implementation instead of three drifting copies. Pure stdlib (``array``
+ ``math``) — no numpy, import-clean everywhere.
"""

from __future__ import annotations

import array
import math


def pack(vector: list[float]) -> bytes:
    """Pack a float vector into a compact little-endian ``float32`` blob."""
    return array.array("f", vector).tobytes()


def unpack(blob: bytes, dims: int) -> list[float]:
    """Inverse of :func:`pack`. ``dims`` guards against trailing padding."""
    arr = array.array("f")
    arr.frombytes(blob)
    return list(arr[:dims])


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in ``[-1, 1]``; ``0.0`` for empty/mismatched inputs."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def l2_normalize(vector: list[float]) -> list[float]:
    """Return the unit-length vector; a zero vector is returned unchanged."""
    norm = math.sqrt(math.fsum(x * x for x in vector))
    if norm == 0.0:
        return list(vector)
    return [x / norm for x in vector]


__all__ = ["cosine", "l2_normalize", "pack", "unpack"]
