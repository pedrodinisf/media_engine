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

PAGE_SIZE = 1000


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


def _iter_artifacts_chunked(
    cache: Any,
    *,
    namespace: str,
    oldest_first: bool,
    page_size: int = PAGE_SIZE,
) -> Iterable[AnyArtifact]:
    """Walk every artifact in this namespace via offset pagination.

    The cache's ``list_artifacts`` defaults to a small limit; eviction
    needs to see the whole table without materializing it all at
    once. We page through ``page_size`` rows at a time in the
    requested order.
    """
    offset = 0
    while True:
        page = cache.list_artifacts(
            limit=page_size,
            offset=offset,
            namespace=namespace,
            oldest_first=oldest_first,
        )
        if not page:
            return
        yield from page
        if len(page) < page_size:
            return
        offset += page_size


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
    function dialect-neutral (SQLite + Postgres alike). The walk is
    paginated so a store with millions of artifacts doesn't blow
    memory.
    """
    if not policy.enabled:
        return EvictionResult(
            bytes_before=0, bytes_after=0, evicted_ids=[], dry_run=dry_run
        )

    max_bytes = int(policy.max_gb * (1024**3))

    # First pass: compute the current total. We do this in a separate
    # walk so the (cache hit, early-return) path doesn't pay for an
    # ascending sort.
    bytes_before = 0
    for art in _iter_artifacts_chunked(
        cache, namespace=namespace, oldest_first=False
    ):
        bytes_before += _artifact_size_bytes(art)

    if bytes_before <= max_bytes:
        return EvictionResult(
            bytes_before=bytes_before,
            bytes_after=bytes_before,
            evicted_ids=[],
            dry_run=dry_run,
        )

    # Second pass: walk OLDEST first, deleting non-protected artifacts
    # until we're back under the cap. The walk has to handle deletes:
    # offset-paginating over a table we're modifying would skip rows
    # (after deleting N rows, ``offset=N`` lands past where we
    # expected). The fix is to keep ``offset`` advancing only by the
    # count of *protected* rows we skipped — non-protected rows are
    # removed, so the next "oldest" naturally re-fills the start of
    # the result set.
    bytes_after = bytes_before
    evicted_ids: list[str] = []
    skipped_protected = 0
    seen_ids: set[str] = set()
    while bytes_after > max_bytes:
        page = cache.list_artifacts(
            limit=PAGE_SIZE,
            offset=skipped_protected,
            namespace=namespace,
            oldest_first=True,
        )
        if not page:
            break
        progressed = False
        for art in page:
            if bytes_after <= max_bytes:
                break
            # Cycle guard: if we've already seen this id and didn't
            # delete it (e.g. dry_run on a protected page), bail to
            # avoid an infinite loop.
            if art.id in seen_ids and dry_run:
                continue
            seen_ids.add(art.id)
            if art.kind in policy.protected_kinds:
                skipped_protected += 1
                progressed = True
                continue
            size = _artifact_size_bytes(art)
            if not dry_run:
                _delete_artifact(cache, art)
            else:
                # Dry-run: also advance past this entry so the next
                # fetch returns the *next* candidate, not the same row.
                skipped_protected += 1
            evicted_ids.append(art.id)
            bytes_after -= size
            progressed = True
        if not progressed:
            break

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
        # 64-char sha256s, no false positives). Also filter by namespace
        # as defense-in-depth: a multi-tenant cache with one namespace
        # per operator must not let one tenant's eviction touch another
        # tenant's run history. The artifact-id primary key already
        # implies one namespace owns the id, but spelling it out keeps
        # the query intent explicit and improves the SQL plan.
        marker = f'"{artifact.id}"'
        s.execute(
            delete(CachedOperationRun).where(
                (CachedOperationRun.namespace == artifact.namespace)
                & (
                    CachedOperationRun.output_ids_json.like(f"%{marker}%")
                    | CachedOperationRun.input_ids_json.like(f"%{marker}%")
                )
            )
        )
        s.execute(
            delete(CachedArtifact).where(CachedArtifact.id == artifact.id)
        )
