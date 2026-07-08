"""Pure fingerprint logic for Phase-7 acoustic speaker identity.

No engine/backend imports — just the deterministic maths behind the "same voice
gets the same stable id" promise, so it unit-tests without any model or DB:

* :func:`stable_speaker_id` — mint a ``Speaker_<sha8>`` label from a centroid.
* :func:`running_mean` — fold new member vectors into an existing centroid.
* :func:`reconcile` — greedy one-to-one match of new clusters to existing
  profiles by cosine ≥ threshold (reuse the id) else mint a new one.

Plus :func:`release_speaker_models`, the RAM-cleanup helper that mirrors
``release_audio_models`` for the ``speaker-embed:`` model-pool slots.
"""

from __future__ import annotations

import gc
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from media_engine.backends._vec import cosine, l2_normalize

if TYPE_CHECKING:
    from media_engine.ops import OperationContext

_ID_PREFIX = "Speaker_"


def stable_speaker_id(centroid: list[float]) -> str:
    """Deterministic ``Speaker_<sha8>`` for a *new* voice.

    Derived from the centroid, rounded to 6 decimals first so trivial
    floating-point noise between runs doesn't fork the id. Cross-run identity
    stability for a *known* voice is handled by :func:`reconcile`, not by this
    hash — this only mints ids for genuinely new clusters.
    """
    rounded = [round(x, 6) for x in centroid]
    digest = hashlib.sha256(
        json.dumps(rounded, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{_ID_PREFIX}{digest[:8]}"


def running_mean(
    old_centroid: list[float],
    old_count: int,
    new_vectors: list[list[float]],
) -> list[float]:
    """Fold ``new_vectors`` into an existing count-weighted centroid.

    ``new = normalize((old * old_count + sum(new_vectors)) / (old_count + n))``.
    The stored ``old_centroid`` is already unit-length; weighting it by its
    member count approximates the true running mean without keeping every
    member vector around. Empty ``new_vectors`` returns the old centroid.
    """
    if not new_vectors:
        return list(old_centroid)
    n = len(new_vectors)
    dims = len(new_vectors[0])
    acc = [old_centroid[i] * old_count for i in range(dims)]
    for vec in new_vectors:
        for i in range(dims):
            acc[i] += vec[i]
    total = old_count + n
    return l2_normalize([x / total for x in acc])


@dataclass(frozen=True)
class ExistingProfile:
    """Minimal view of a persisted profile for reconciliation."""

    speaker_id: str
    centroid: list[float]
    member_count: int


@dataclass(frozen=True)
class ReconcileDecision:
    """Outcome for one new cluster: reuse an id or mint a fresh one."""

    speaker_id: str
    reused: bool
    best_score: float
    matched_existing: ExistingProfile | None


def reconcile(
    new_centroids: list[list[float]],
    existing: list[ExistingProfile],
    threshold: float,
) -> list[ReconcileDecision]:
    """Greedy one-to-one assignment of new clusters to existing profiles.

    Every ``(new_i, existing_j)`` pair is scored by cosine and considered in
    descending order; a pair is bound only if *both* sides are still free and
    the score ≥ ``threshold``. Bound new clusters **reuse** the existing
    ``speaker_id``; unbound ones **mint** a new id from their centroid. This is
    deterministic (ties break by the fixed enumeration order) and never maps two
    new clusters onto the same existing voice within a run.
    """
    decisions: list[ReconcileDecision | None] = [None] * len(new_centroids)
    best_scores = [0.0] * len(new_centroids)

    pairs: list[tuple[float, int, int]] = []
    for i, cen in enumerate(new_centroids):
        for j, prof in enumerate(existing):
            s = cosine(cen, prof.centroid)
            if s > best_scores[i]:
                best_scores[i] = s
            pairs.append((s, i, j))
    # Highest score first; stable tie-break on (i, j) to stay deterministic.
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

    claimed_new: set[int] = set()
    claimed_existing: set[int] = set()
    for score, i, j in pairs:
        if score < threshold:
            break
        if i in claimed_new or j in claimed_existing:
            continue
        prof = existing[j]
        decisions[i] = ReconcileDecision(
            speaker_id=prof.speaker_id,
            reused=True,
            best_score=score,
            matched_existing=prof,
        )
        claimed_new.add(i)
        claimed_existing.add(j)

    out: list[ReconcileDecision] = []
    for i, cen in enumerate(new_centroids):
        d = decisions[i]
        if d is not None:
            out.append(d)
        else:
            out.append(
                ReconcileDecision(
                    speaker_id=stable_speaker_id(cen),
                    reused=False,
                    best_score=best_scores[i],
                    matched_existing=None,
                )
            )
    return out


def release_speaker_models(ctx: OperationContext | None = None) -> None:
    """Drop cached voice-embedding models from the pool + reclaim RAM.

    Mirrors ``release_audio_models``: forget every ``speaker-embed:`` slot in
    ``ctx.model_pool`` (the key prefix used by the pyannote embedding backend),
    then nudge Apple Silicon's unified-memory allocator with
    ``mx.clear_cache`` + ``gc.collect`` so the bytes are promptly reclaimable.
    Best-effort and silent on missing optional deps.
    """
    if ctx is not None and ctx.model_pool is not None:
        for key in list(ctx.model_pool.keys()):
            if key.startswith("speaker-embed:"):
                ctx.model_pool.forget(key)

    try:
        import mlx.core as mx_module  # type: ignore[import]
        mx: Any = mx_module
        if hasattr(mx, "clear_cache"):
            mx.clear_cache()
        elif hasattr(mx, "metal") and hasattr(mx.metal, "clear_cache"):
            mx.metal.clear_cache()
    except ImportError:
        pass

    gc.collect()


__all__ = [
    "ExistingProfile",
    "ReconcileDecision",
    "reconcile",
    "release_speaker_models",
    "running_mean",
    "stable_speaker_id",
]
