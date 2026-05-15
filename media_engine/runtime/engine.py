"""``Engine`` — public Python API (Phase 0 commit 3 read-only surface).

Ships ``open_quick`` / ``open_session`` factories plus the read-only
artifact / lineage / list / resolve-id surface. ``Engine.run`` arrives in
commit 7 once the operation registry exists. The DAG executor lands in
Phase 1 commit 14.
"""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Self

from media_engine.artifacts import AnyArtifact, Kind
from media_engine.config import EngineConfig
from media_engine.runtime.cache import Cache
from media_engine.runtime.lineage import LineageNode
from media_engine.runtime.storage import LocalFSStorage, StorageBackend


class Engine:
    """Public engine handle. Use ``open_quick()`` or ``open_session()``."""

    def __init__(
        self,
        config: EngineConfig,
        cache: Cache,
        storage: StorageBackend,
    ) -> None:
        self.config = config
        self.cache = cache
        self.storage = storage

    @classmethod
    def open_quick(cls, config: EngineConfig | None = None) -> Self:
        """Stateless one-shot. SQLite open + storage validation. No model loads."""
        cfg = config or EngineConfig.load()
        cfg.validate_storage()
        cache = Cache(cfg.resolve_cache_db_url())
        storage = LocalFSStorage(cfg.permanent_store, cfg.workdir)
        return cls(cfg, cache, storage)

    @classmethod
    def open_session(cls, config: EngineConfig | None = None) -> Self:
        """Long-lived session. Phase 1 adds warm model pool, semaphores, server
        lifecycle. For now identical to ``open_quick``."""
        return cls.open_quick(config)

    def get_artifact(self, artifact_id: str) -> AnyArtifact | None:
        return self.cache.get_artifact(artifact_id, namespace=self.config.namespace)

    def list_artifacts(
        self,
        kind: Kind | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[AnyArtifact]:
        return self.cache.list_artifacts(
            kind=kind, since=since, limit=limit, namespace=self.config.namespace
        )

    def lineage(self, artifact_id: str, max_depth: int = 10) -> LineageNode | None:
        return self.cache.lineage_tree(
            artifact_id, namespace=self.config.namespace, max_depth=max_depth
        )

    def resolve_id(self, prefix: str) -> str:
        """Git-style prefix → full sha256. Raises on miss or ambiguity."""
        matches = self.cache.resolve_id_prefix(prefix, namespace=self.config.namespace)
        if not matches:
            raise LookupError(f"No artifact id starting with {prefix!r}")
        if len(matches) > 1:
            preview = ", ".join(m[:12] for m in matches[:5])
            raise LookupError(
                f"Ambiguous prefix {prefix!r}: matches {len(matches)} ids "
                f"(e.g. {preview})"
            )
        return matches[0]

    def close(self) -> None:
        self.cache.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
