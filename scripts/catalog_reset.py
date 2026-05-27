#!/usr/bin/env python3
"""One-shot helper: delete every artifact whose kind is not in a
whitelist from the live cache + permanent store.

Use case: a `video.comprehend` run littered the catalog with per-frame
FrameSets + per-frame Analyses, and you just want the source Audios
and Videos back to a clean slate.

Usage:
    uv run python scripts/catalog_reset.py --keep video,audio --apply
    uv run python scripts/catalog_reset.py --keep video,audio          # dry-run

Reads the same ``EngineConfig`` your CLI / Web UI use, so namespace +
permanent_store + cache_db_url are picked up automatically.

Affects only the configured namespace. Operations runs that reference
the deleted artifacts (as input OR output) are also pruned so the cost
ledger doesn't grow stale "ghost" references.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make ``media_engine`` importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_engine.artifacts import Kind  # noqa: E402
from media_engine.config import EngineConfig  # noqa: E402
from media_engine.runtime.cache import (  # noqa: E402
    Cache,
    CachedArtifact,
    CachedOperationRun,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        default="video,audio",
        help=(
            "Comma-separated list of kinds to PRESERVE "
            "(everything else is deleted). Default: video,audio"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this flag, prints what would be deleted.",
    )
    args = parser.parse_args()

    keep_kinds: set[str] = {k.strip().lower() for k in args.keep.split(",") if k.strip()}
    valid = {k.value for k in Kind}
    unknown = keep_kinds - valid
    if unknown:
        print(f"ERROR: unknown kind(s) in --keep: {sorted(unknown)}", file=sys.stderr)
        print(f"  valid: {sorted(valid)}", file=sys.stderr)
        return 2

    config = EngineConfig()
    # ``cache_db_url`` is None by default; the Engine resolves it to
    # ``sqlite+pysqlite:///<permanent_store>/cache.db``. Use the same
    # resolver so this script targets the exact DB the running engine
    # sees, regardless of whether MEDIA_ENGINE_CACHE_DB_URL is set.
    cache_url = config.resolve_cache_db_url()
    cache = Cache(cache_url)
    namespace = config.namespace

    print(f"Cache:       {cache_url}")
    print(f"Store:       {config.permanent_store}")
    print(f"Namespace:   {namespace}")
    print(f"Keep kinds:  {sorted(keep_kinds)}")
    print(f"Mode:        {'APPLY (will delete)' if args.apply else 'DRY-RUN (no changes)'}")
    print()

    # Group counts by kind so the user sees what's about to vanish.
    from sqlalchemy import delete, func, select

    with cache.session() as s:
        kind_counts = dict(
            s.execute(
                select(CachedArtifact.kind, func.count(CachedArtifact.id))
                .where(CachedArtifact.namespace == namespace)
                .group_by(CachedArtifact.kind)
            ).all()
        )

    if not kind_counts:
        print("(no artifacts in this namespace — nothing to do)")
        return 0

    print(f"{'kind':<14}{'count':>8}   action")
    print("─" * 40)
    total_delete = 0
    for kind, n in sorted(kind_counts.items()):
        action = "keep" if kind in keep_kinds else "DELETE"
        if kind not in keep_kinds:
            total_delete += n
        print(f"{kind:<14}{n:>8}   {action}")
    print("─" * 40)
    print(f"To delete:   {total_delete}")
    print()

    if not args.apply:
        print("Dry-run complete. Re-run with `--apply` to actually delete.")
        return 0

    if total_delete == 0:
        print("Nothing to delete.")
        return 0

    # Phase 1: collect the doomed rows so we can unlink files on disk
    # AND delete the SQL rows.
    print("[1/3] Collecting doomed artifact ids + paths…")
    with cache.session() as s:
        doomed = list(
            s.execute(
                select(CachedArtifact.id, CachedArtifact.path)
                .where(CachedArtifact.namespace == namespace)
                .where(~CachedArtifact.kind.in_(keep_kinds))
            ).all()
        )
    print(f"        {len(doomed)} rows")

    # Phase 2: unlink files.
    print("[2/3] Unlinking files…")
    unlinked = 0
    for _aid, path in doomed:
        p = Path(path)
        try:
            p.unlink(missing_ok=True)
            unlinked += 1
        except OSError as e:
            print(f"        warning: could not unlink {p}: {e}", file=sys.stderr)
    print(f"        {unlinked} files unlinked")

    # Phase 3: delete cache rows + operation-run rows that reference them.
    print("[3/3] Deleting cache rows…")
    doomed_ids = {aid for aid, _p in doomed}
    with cache.session() as s:
        # Drop op runs that reference any doomed id (as input OR
        # output). Use the same LIKE-on-JSON pattern as eviction.py
        # because we don't have an indexed join here.
        for aid in doomed_ids:
            marker = f'"{aid}"'
            s.execute(
                delete(CachedOperationRun).where(
                    CachedOperationRun.namespace == namespace,
                    (
                        CachedOperationRun.input_ids_json.like(f"%{marker}%")
                        | CachedOperationRun.output_ids_json.like(f"%{marker}%")
                    ),
                )
            )
        # Drop the artifacts themselves.
        s.execute(
            delete(CachedArtifact)
            .where(CachedArtifact.namespace == namespace)
            .where(~CachedArtifact.kind.in_(keep_kinds))
        )
    print(f"        {len(doomed_ids)} artifact rows deleted")
    print()
    print("Done. Reload the catalog page in the UI to see the reset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
