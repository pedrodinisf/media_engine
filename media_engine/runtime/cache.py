"""Content-addressed cache for artifacts and operation runs.

SQLAlchemy 2.0 declarative. SQLite (Phases 0–3) with WAL pragmas; Postgres
support arrives in Phase 4 via the same ORM. Two tables this commit:
``cached_artifacts`` and ``cached_operation_runs``. ``jobs`` lands in Phase 4.

The Pydantic ↔ SQLAlchemy boundary is explicit: ``to_orm()`` and
``to_pydantic()`` are the only crossings. ORM rows own durable state; Pydantic
artifacts are the immutable in-flight representation that ops produce/consume.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.engine import Engine as SAEngine
from sqlalchemy.event import listens_for
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import Pool

from media_engine.artifacts import AnyArtifact, Kind
from media_engine.artifacts.analysis import Analysis, Embedding, SessionAnalysis
from media_engine.artifacts.media import Audio, FrameSet, Image, Video
from media_engine.artifacts.text import (
    Chunks,
    Diarization,
    Document,
    MarkdownArtifact,
    OCRText,
    Transcript,
    WebPage,
)
from media_engine.runtime.lineage import LineageNode, OperationRunRef

# Map Kind enum → concrete Pydantic class (for to_pydantic dispatch).
_KIND_TO_CLASS: dict[Kind, type[AnyArtifact]] = {
    Kind.Video: Video,
    Kind.Audio: Audio,
    Kind.Image: Image,
    Kind.FrameSet: FrameSet,
    Kind.Transcript: Transcript,
    Kind.Diarization: Diarization,
    Kind.OCRText: OCRText,
    Kind.Chunks: Chunks,
    Kind.Embedding: Embedding,
    Kind.Analysis: Analysis,
    Kind.SessionAnalysis: SessionAnalysis,
    Kind.MarkdownArtifact: MarkdownArtifact,
    Kind.Document: Document,
    Kind.WebPage: WebPage,
}


class Base(DeclarativeBase):
    """Declarative base for all cache tables."""


class CachedArtifact(Base):
    __tablename__ = "cached_artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    derived_from_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    produced_by: Mapped[str | None] = mapped_column(String, nullable=True)
    namespace: Mapped[str] = mapped_column(String, nullable=False, default="default")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "namespace", name="uq_artifact_id_namespace"),
        Index("idx_artifacts_kind", "kind", "created_at"),
    )


class CachedOperationRun(Base):
    __tablename__ = "cached_operation_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    op_name: Mapped[str] = mapped_column(String, nullable=False)
    op_version: Mapped[str] = mapped_column(String, nullable=False)
    backend_name: Mapped[str | None] = mapped_column(String, nullable=True)
    backend_version: Mapped[str | None] = mapped_column(String, nullable=True)
    params_hash: Mapped[str] = mapped_column(String, nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    input_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    output_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    cost_estimate_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_cost_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    namespace: Mapped[str] = mapped_column(String, nullable=False, default="default")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "op_name",
            "op_version",
            "backend_name",
            "backend_version",
            "params_hash",
            "input_ids_json",
            "namespace",
            name="uq_operation_runs_lookup",
        ),
        Index(
            "idx_runs_lookup",
            "op_name",
            "op_version",
            "backend_name",
            "backend_version",
            "params_hash",
            "input_ids_json",
        ),
    )


# ─────────────────────────────────────────────────────────────────
# Pydantic ↔ SQLAlchemy boundary (the only crossings)
# ─────────────────────────────────────────────────────────────────


def _ensure_utc(dt: datetime) -> datetime:
    """Naive datetimes from SQLite are assumed UTC (the engine writes UTC)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def to_orm(artifact: AnyArtifact) -> CachedArtifact:
    return CachedArtifact(
        id=artifact.id,
        kind=artifact.kind.value,
        path=str(artifact.path),
        metadata_json=json.dumps(artifact.metadata, sort_keys=True),
        derived_from_json=json.dumps(list(artifact.derived_from)),
        produced_by=artifact.produced_by,
        namespace=artifact.namespace,
        created_at=_ensure_utc(artifact.created_at),
    )


def to_pydantic(row: CachedArtifact) -> AnyArtifact:
    kind = Kind(row.kind)
    cls = _KIND_TO_CLASS[kind]
    return cls(
        id=row.id,
        path=row.path,  # type: ignore[arg-type]
        metadata=json.loads(row.metadata_json),
        derived_from=tuple(json.loads(row.derived_from_json)),
        produced_by=row.produced_by,
        namespace=row.namespace,
        created_at=_ensure_utc(row.created_at),
    )


# ─────────────────────────────────────────────────────────────────
# Cache class — the engine-facing interface
# ─────────────────────────────────────────────────────────────────


class Cache:
    """Cache facade. Wraps a SQLAlchemy Engine + Session factory.

    On open against a SQLite URL, applies WAL pragmas via a connection-event hook
    (per-connection, runs once each new connection).
    """

    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self.engine: SAEngine = create_engine(db_url, future=True)
        self._is_sqlite = db_url.startswith("sqlite")
        if self._is_sqlite:
            _attach_sqlite_pragmas(self.engine.pool)
        Base.metadata.create_all(self.engine)
        self._session = sessionmaker(bind=self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        s = self._session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ── artifact storage ──

    def upsert_artifact(self, artifact: AnyArtifact) -> None:
        with self.session() as s:
            existing = s.get(CachedArtifact, artifact.id)
            if existing is not None and existing.namespace == artifact.namespace:
                return
            s.add(to_orm(artifact))

    def get_artifact(
        self, artifact_id: str, namespace: str = "default"
    ) -> AnyArtifact | None:
        with self.session() as s:
            row = s.get(CachedArtifact, artifact_id)
            if row is None or row.namespace != namespace:
                return None
            return to_pydantic(row)

    def list_artifacts(
        self,
        kind: Kind | None = None,
        since: datetime | None = None,
        limit: int = 100,
        namespace: str = "default",
    ) -> list[AnyArtifact]:
        with self.session() as s:
            stmt = select(CachedArtifact).where(CachedArtifact.namespace == namespace)
            if kind is not None:
                stmt = stmt.where(CachedArtifact.kind == kind.value)
            if since is not None:
                stmt = stmt.where(CachedArtifact.created_at >= since)
            stmt = stmt.order_by(CachedArtifact.created_at.desc()).limit(limit)
            return [to_pydantic(r) for r in s.scalars(stmt).all()]

    def resolve_id_prefix(
        self, prefix: str, namespace: str = "default"
    ) -> list[str]:
        """Git-style prefix resolution. Returns list of full ids that match."""
        if len(prefix) < 4:
            raise ValueError(f"prefix must be at least 4 chars (got {len(prefix)!r})")
        with self.session() as s:
            stmt = select(CachedArtifact.id).where(
                CachedArtifact.namespace == namespace,
                CachedArtifact.id.like(f"{prefix}%"),
            )
            return list(s.scalars(stmt).all())

    # ── operation runs (cache lookup) ──

    def find_cached_run(
        self,
        *,
        op_name: str,
        op_version: str,
        backend_name: str | None,
        backend_version: str | None,
        params_hash: str,
        input_ids: Iterable[str],
        namespace: str = "default",
    ) -> list[str] | None:
        """Return output artifact ids if a matching run exists, else None."""
        sorted_inputs = sorted(input_ids)
        input_ids_json = json.dumps(sorted_inputs)
        with self.session() as s:
            stmt = select(CachedOperationRun).where(
                CachedOperationRun.op_name == op_name,
                CachedOperationRun.op_version == op_version,
                CachedOperationRun.backend_name.is_(backend_name)
                if backend_name is None
                else CachedOperationRun.backend_name == backend_name,
                CachedOperationRun.backend_version.is_(backend_version)
                if backend_version is None
                else CachedOperationRun.backend_version == backend_version,
                CachedOperationRun.params_hash == params_hash,
                CachedOperationRun.input_ids_json == input_ids_json,
                CachedOperationRun.namespace == namespace,
            )
            row = s.scalars(stmt).first()
            if row is None:
                return None
            return list(json.loads(row.output_ids_json))

    def record_run(
        self,
        *,
        op_name: str,
        op_version: str,
        backend_name: str | None,
        backend_version: str | None,
        params: dict[str, Any],
        params_hash: str,
        input_ids: Iterable[str],
        output_ids: Iterable[str],
        cost_estimate: dict[str, Any] | None,
        actual_cost: dict[str, Any] | None,
        duration_seconds: float | None,
        started_at: datetime,
        finished_at: datetime,
        namespace: str = "default",
    ) -> str:
        """Insert an operation_run row. Returns the run id (uuid hex)."""
        run_id = uuid4().hex
        sorted_inputs = sorted(input_ids)
        with self.session() as s:
            s.add(
                CachedOperationRun(
                    id=run_id,
                    op_name=op_name,
                    op_version=op_version,
                    backend_name=backend_name,
                    backend_version=backend_version,
                    params_hash=params_hash,
                    params_json=json.dumps(params, sort_keys=True),
                    input_ids_json=json.dumps(sorted_inputs),
                    output_ids_json=json.dumps(list(output_ids)),
                    cost_estimate_json=(
                        json.dumps(cost_estimate, sort_keys=True)
                        if cost_estimate is not None
                        else None
                    ),
                    actual_cost_json=(
                        json.dumps(actual_cost, sort_keys=True)
                        if actual_cost is not None
                        else None
                    ),
                    duration_seconds=duration_seconds,
                    namespace=namespace,
                    started_at=_ensure_utc(started_at),
                    finished_at=_ensure_utc(finished_at),
                )
            )
        return run_id

    # ── lineage ──

    def parents_of(
        self, artifact_id: str, namespace: str = "default"
    ) -> list[AnyArtifact]:
        artifact = self.get_artifact(artifact_id, namespace=namespace)
        if artifact is None:
            return []
        out: list[AnyArtifact] = []
        for pid in artifact.derived_from:
            p = self.get_artifact(pid, namespace=namespace)
            if p is not None:
                out.append(p)
        return out

    def get_run(self, run_id: str) -> OperationRunRef | None:
        with self.session() as s:
            row = s.get(CachedOperationRun, run_id)
            if row is None:
                return None
            return OperationRunRef(
                id=row.id,
                op_name=row.op_name,
                op_version=row.op_version,
                backend_name=row.backend_name,
                backend_version=row.backend_version,
                started_at=_ensure_utc(row.started_at),
                finished_at=_ensure_utc(row.finished_at),
                duration_seconds=row.duration_seconds,
                params=json.loads(row.params_json),
            )

    def lineage_tree(
        self,
        artifact_id: str,
        namespace: str = "default",
        max_depth: int = 10,
    ) -> LineageNode | None:
        """Walk upstream from ``artifact_id``. Cycle-safe, depth-limited."""
        return self._build_lineage(artifact_id, namespace, max_depth, seen=set())

    def _build_lineage(
        self,
        artifact_id: str,
        namespace: str,
        depth_remaining: int,
        seen: set[str],
    ) -> LineageNode | None:
        if artifact_id in seen:
            return None
        seen = seen | {artifact_id}
        artifact = self.get_artifact(artifact_id, namespace=namespace)
        if artifact is None:
            return None
        op_run = self.get_run(artifact.produced_by) if artifact.produced_by else None
        parents: list[LineageNode] = []
        if depth_remaining > 0:
            for pid in artifact.derived_from:
                child = self._build_lineage(pid, namespace, depth_remaining - 1, seen)
                if child is not None:
                    parents.append(child)
        return LineageNode(artifact=artifact, op_run=op_run, parents=parents)

    def close(self) -> None:
        self.engine.dispose()


# ─────────────────────────────────────────────────────────────────
# SQLite pragmas — WAL + busy_timeout for concurrent CLI invocations
# ─────────────────────────────────────────────────────────────────


def _attach_sqlite_pragmas(pool: Pool) -> None:
    def _set_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    listens_for(pool, "connect")(_set_pragmas)
