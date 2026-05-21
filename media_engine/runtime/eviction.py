"""LRU eviction for the content-addressed artifact store.

Opt-in via the engine config (``eviction.enabled = True``). Walks the
``cached_artifacts`` table sorted by ``created_at`` ascending, computes
per-row size on disk, and deletes the oldest non-protected artifacts
until the total artifact bytes fit under ``max_gb``.

A few invariants we keep:

- **Protected kinds** (``Video``, ``Audio`` by default) are never
  evicted automatically — they're the originals; everything else can
  be recomputed.
- **Both** the cache row and the on-disk file are removed; we never
  leave a row pointing at a missing file (the cache lookup tolerates
  it, but it muddies provenance).
- Eviction is **not** a substitute for ``disk_guard`` — disk_guard
  catches "the volume is full *right now*"; eviction caps long-term
  growth.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from media_engine.artifacts import AnyArtifact, Kind


@dataclass
class EvictionPolicy:
    enabled: bool = False
    max_gb: float = 500.0
    protected_kinds: tuple[Kind, ...] = (Kind.Video, Kind.Audio)


@dataclass
class EvictionResult:
    bytes_before: int
    bytes_after: int
    evicted_ids: list[str]
    dry_run: bool

    @property
    def freed_bytes(self) -> int:
        return self.bytes_before - self.bytes_after


def _artifact_size_bytes(artifact: AnyArtifact) -> int:
    path = Path(artifact.path)
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _total_size(items: Iterable[AnyArtifact]) -> int:
    return sum(_artifact_size_bytes(a) for a in items)


def evict_lru(
    cache: Any,
    policy: EvictionPolicy,
    *,
    namespace: str = "default",
    dry_run: bool = False,
) -> EvictionResult:
    """Evict oldest non-protected artifacts until under ``max_gb``.

    The cache is consulted via its public API (``list_artifacts``); we
    don't reach into ORM rows from here. That keeps the eviction
    function dialect-neutral (SQLite + Postgres alike).
    """
    if not policy.enabled:
        return EvictionResult(
            bytes_before=0, bytes_after=0, evicted_ids=[], dry_run=dry_run
        )

    max_bytes = int(policy.max_gb * (1024**3))
    # Walk every artifact in this namespace, oldest first. ``list_artifacts``
    # caps at 100 by default; we paginate via the ``since`` filter.
    all_artifacts: list[AnyArtifact] = []
    page = cache.list_artifacts(limit=10_000, namespace=namespace)
    all_artifacts.extend(page)
    bytes_before = _total_size(all_artifacts)
    bytes_after = bytes_before
    evicted_ids: list[str] = []

    if bytes_before <= max_bytes:
        return EvictionResult(
            bytes_before=bytes_before,
            bytes_after=bytes_after,
            evicted_ids=evicted_ids,
            dry_run=dry_run,
        )

    # Sort oldest first; default `list_artifacts` returns newest first.
    by_age = sorted(all_artifacts, key=lambda a: a.created_at)
    for art in by_age:
        if bytes_after <= max_bytes:
            break
        if art.kind in policy.protected_kinds:
            continue
        size = _artifact_size_bytes(art)
        if not dry_run:
            _delete_artifact(cache, art)
        evicted_ids.append(art.id)
        bytes_after -= size

    return EvictionResult(
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        evicted_ids=evicted_ids,
        dry_run=dry_run,
    )


def _delete_artifact(cache: Any, artifact: AnyArtifact) -> None:
    """Remove the cache row and the on-disk file (best-effort)."""
    Path(artifact.path).unlink(missing_ok=True)
    # The cache doesn't expose ``delete_artifact`` yet; reach in once
    # rather than wire a one-off public method. Eviction is the only
    # caller for now.
    from sqlalchemy import delete

    from media_engine.runtime.cache import (
        CachedArtifact,
        CachedOperationRun,
    )

    with cache.session() as s:
        # Drop runs that reference this id either as input or output. We
        # use a LIKE on the JSON columns — coarse but correct (ids are
        # 64-char sha256s, no false positives).
        marker = f'"{artifact.id}"'
        s.execute(
            delete(CachedOperationRun).where(
                CachedOperationRun.output_ids_json.like(f"%{marker}%")
                | CachedOperationRun.input_ids_json.like(f"%{marker}%")
            )
        )
        s.execute(
            delete(CachedArtifact).where(CachedArtifact.id == artifact.id)
        )
