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

from pydantic import BaseModel
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
from media_engine.runtime.lineage import LineageNode, OperationRunRef, TruncatedReason

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


class CostLogEntry(Base):
    """Append-only spend ledger — one row per *actual* op execution.

    Distinct from ``cached_operation_runs`` (which is keyed by the cache
    lookup tuple and upserted, so a re-run overwrites history): this table
    keeps every execution so ``med cost`` can report true spend over time.
    Cache hits are never logged here (they cost nothing).
    """

    __tablename__ = "cost_log"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    op_name: Mapped[str] = mapped_column(String, nullable=False)
    backend_name: Mapped[str | None] = mapped_column(String, nullable=True)
    namespace: Mapped[str] = mapped_column(
        String, nullable=False, default="default"
    )
    estimated_cents: Mapped[float] = mapped_column(Float, default=0.0)
    actual_cents: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    duration_seconds: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    __table_args__ = (
        Index("idx_cost_log_ts", "ts"),
        Index("idx_cost_log_op", "op_name"),
    )


class EventLogEntry(Base):
    """Persisted engine events — backs ``med events history``.

    The live stream is in-process (``EventBus``); this table is the
    durable tail. Rotated weekly (``Cache.prune_events``) so it doesn't
    grow without bound.
    """

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    op_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    op_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Phase 6.5 (B-001 fix): job_id is the REST/CLI submission id, distinct
    # from op_run_id (which is per-op-execution). A pipeline job has many
    # op_runs sharing one job_id; a single-op REST job has job_id ≠
    # op_run_id (the engine generates op_run_id internally). Indexed so
    # SSE replay can ``WHERE job_id = ?`` cheaply.
    job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    namespace: Mapped[str] = mapped_column(
        String, nullable=False, default="default"
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_events_ts", "ts"),
        Index("idx_events_run", "op_run_id"),
        Index("idx_events_job", "job_id"),
    )


class JobRow(Base):
    """REST-submitted unit of work (single op or compiled pipeline).

    The REST surface in Phase 4 is async-first: ``POST /run`` /
    ``POST /pipelines`` accept work and return a ``job_id`` immediately;
    the actual op execution runs in an asyncio background task whose
    progress is observable via ``GET /jobs/{id}`` and
    ``GET /jobs/{id}/events`` (SSE). One row per submission; the
    ``op_run_ids`` and ``output_artifact_ids`` JSON columns accumulate
    as ops complete inside the job.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    pipeline_name: Mapped[str | None] = mapped_column(String, nullable=True)
    pipeline_yaml: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    op_run_ids_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    output_artifact_ids_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    namespace: Mapped[str] = mapped_column(
        String, nullable=False, default="default"
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_jobs_status", "status"),
        Index("idx_jobs_submitted", "submitted_at"),
    )


class ApiToken(Base):
    """Bearer tokens for the REST surface.

    Tokens are hashed at rest (sha256 of the raw 32-byte secret). The
    raw secret is returned exactly once at creation time; the server
    only ever compares hashes thereafter. ``label`` is a human-readable
    name (e.g. ``"laptop-cli"``).
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    token_hash: Mapped[str] = mapped_column(
        String, nullable=False, unique=True
    )
    label: Mapped[str] = mapped_column(String, nullable=False, default="")
    namespace: Mapped[str] = mapped_column(
        String, nullable=False, default="default"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("idx_tokens_hash", "token_hash"),)


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


class Job(BaseModel):
    """REST job — one async unit of work submitted via the API.

    A job wraps either a single op (``POST /run``) or a compiled pipeline
    (``POST /pipelines``). The row is created in ``pending`` immediately on
    submission so the client always has an id to poll; the background
    runner flips it to ``running`` and finally to ``completed`` /
    ``failed`` / ``cancelled``.
    """

    id: str
    pipeline_name: str | None = None
    status: str  # pending | running | completed | failed | cancelled
    op_run_ids: list[str] = []
    output_artifact_ids: list[str] = []
    namespace: str = "default"
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: dict[str, Any] | None = None


class ApiTokenInfo(BaseModel):
    """Public projection of an API token row (no hash, no secret)."""

    id: str
    label: str
    namespace: str
    created_at: datetime
    revoked_at: datetime | None = None


def _job_from_row(row: JobRow) -> Job:
    return Job(
        id=row.id,
        pipeline_name=row.pipeline_name,
        status=row.status,
        op_run_ids=list(json.loads(row.op_run_ids_json)),
        output_artifact_ids=list(json.loads(row.output_artifact_ids_json)),
        namespace=row.namespace,
        submitted_at=_ensure_utc(row.submitted_at),
        started_at=(
            _ensure_utc(row.started_at) if row.started_at else None
        ),
        finished_at=(
            _ensure_utc(row.finished_at) if row.finished_at else None
        ),
        error=json.loads(row.error_json) if row.error_json else None,
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
        """Insert the artifact row (idempotent in the same namespace).

        ``cached_artifacts.id`` is the primary key, so the same id can
        only belong to one namespace at a time — the table is
        content-addressed across the whole store, not per-tenant. We
        surface that as a clear ``ValueError`` instead of falling
        through to a SQL ``IntegrityError`` later, which is what would
        happen if two tenants tried to register the same bytes.
        """
        with self.session() as s:
            existing = s.get(CachedArtifact, artifact.id)
            if existing is not None:
                if existing.namespace == artifact.namespace:
                    return
                raise ValueError(
                    f"artifact id {artifact.id[:12]}… already exists in "
                    f"namespace {existing.namespace!r}; cannot also register "
                    f"under {artifact.namespace!r} (one namespace per id)"
                )
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
        offset: int = 0,
        namespace: str = "default",
        oldest_first: bool = False,
        include_ephemeral: bool = False,
    ) -> list[AnyArtifact]:
        """List artifacts in the cache.

        ``oldest_first`` flips the sort direction; the default is
        newest-first (what `med ls` and `GET /artifacts` want).
        Eviction passes ``oldest_first=True`` so it sees the long
        tail even when the table is large.

        ``include_ephemeral`` defaults to False — internal scaffolding
        artifacts (today: the per-frame FrameSet manifests created by
        ``video.comprehend``'s fan-out) are hidden from catalog
        listings. Set to True to debug-inspect them. The filter uses
        substring matches on the canonical sorted-keys JSON text of
        ``metadata_json``, so it works portably across SQLite and
        Postgres without DB-specific JSON operators. Two shapes are
        excluded:

          * The new ``metadata.ephemeral = true`` flag.
          * Legacy single-frame FrameSets (pre-flag): rows that match
            ``kind = 'frameset' AND metadata.parent_position`` exists.
            Cleans up existing stores without a data migration.
        """
        with self.session() as s:
            stmt = select(CachedArtifact).where(CachedArtifact.namespace == namespace)
            if kind is not None:
                stmt = stmt.where(CachedArtifact.kind == kind.value)
            if since is not None:
                stmt = stmt.where(CachedArtifact.created_at >= since)
            if not include_ephemeral:
                stmt = stmt.where(
                    ~CachedArtifact.metadata_json.like('%"ephemeral": true%')
                )
                stmt = stmt.where(
                    ~(
                        (CachedArtifact.kind == Kind.FrameSet.value)
                        & CachedArtifact.metadata_json.like('%"parent_position":%')
                    )
                )
            order = (
                CachedArtifact.created_at.asc()
                if oldest_first
                else CachedArtifact.created_at.desc()
            )
            stmt = stmt.order_by(order).offset(offset).limit(limit)
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

    # ── cost ledger ──

    def record_cost(
        self,
        *,
        op_name: str,
        backend_name: str | None,
        estimated_cents: float,
        actual_cents: float,
        tokens_in: int,
        tokens_out: int,
        duration_seconds: float | None,
        namespace: str = "default",
        ts: datetime | None = None,
    ) -> None:
        """Append one execution to the spend ledger."""
        with self.session() as s:
            s.add(
                CostLogEntry(
                    id=uuid4().hex,
                    ts=_ensure_utc(ts or datetime.now(UTC)),
                    op_name=op_name,
                    backend_name=backend_name,
                    namespace=namespace,
                    estimated_cents=estimated_cents,
                    actual_cents=actual_cents,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    duration_seconds=duration_seconds,
                )
            )

    def cost_log(
        self,
        *,
        since: datetime | None = None,
        op_name: str | None = None,
        namespace: str | None = None,
        limit: int | None = None,
    ) -> list[CostLogEntry]:
        """Return ledger rows newest-first, optionally filtered."""
        with self.session() as s:
            stmt = select(CostLogEntry).order_by(CostLogEntry.ts.desc())
            if since is not None:
                stmt = stmt.where(CostLogEntry.ts >= _ensure_utc(since))
            if op_name is not None:
                stmt = stmt.where(CostLogEntry.op_name == op_name)
            if namespace is not None:
                stmt = stmt.where(CostLogEntry.namespace == namespace)
            if limit is not None:
                stmt = stmt.limit(limit)
            return list(s.scalars(stmt).all())

    # ── event log ──

    def record_event(
        self,
        *,
        ts: datetime,
        event_type: str,
        op_run_id: str | None,
        op_name: str | None,
        payload_json: str,
        namespace: str = "default",
        job_id: str | None = None,
        event_id: str | None = None,
    ) -> None:
        """Append one event to the durable tail (best-effort sink).

        ``event_id`` — if provided, used as the row PK so the persisted
        id matches ``Event.event_id`` exactly. This lets the SSE pumper
        dedup live vs replayed events by id without parsing payloads.
        """
        with self.session() as s:
            s.add(
                EventLogEntry(
                    id=event_id or uuid4().hex,
                    ts=_ensure_utc(ts),
                    type=event_type,
                    op_run_id=op_run_id,
                    op_name=op_name,
                    job_id=job_id,
                    namespace=namespace,
                    payload_json=payload_json,
                )
            )

    def event_log(
        self,
        *,
        since: datetime | None = None,
        op_run_id: str | None = None,
        job_id: str | None = None,
        namespace: str | None = None,
        limit: int | None = None,
        order: str = "desc",
    ) -> list[EventLogEntry]:
        """Return persisted events optionally filtered.

        ``order`` — ``"desc"`` (newest-first, default — matches
        ``med events history`` UX) or ``"asc"`` (chronological — what
        SSE replay needs to preserve causal order).
        """
        with self.session() as s:
            stmt = select(EventLogEntry)
            stmt = stmt.order_by(
                EventLogEntry.ts.desc() if order == "desc" else EventLogEntry.ts.asc()
            )
            if since is not None:
                stmt = stmt.where(EventLogEntry.ts >= _ensure_utc(since))
            if op_run_id is not None:
                stmt = stmt.where(EventLogEntry.op_run_id == op_run_id)
            if job_id is not None:
                stmt = stmt.where(EventLogEntry.job_id == job_id)
            if namespace is not None:
                stmt = stmt.where(EventLogEntry.namespace == namespace)
            if limit is not None:
                stmt = stmt.limit(limit)
            return list(s.scalars(stmt).all())

    # ── jobs (Phase 4 REST surface) ──

    def insert_job(
        self,
        *,
        job_id: str,
        pipeline_name: str | None,
        pipeline_yaml: str | None,
        namespace: str = "default",
        submitted_at: datetime | None = None,
    ) -> None:
        with self.session() as s:
            s.add(
                JobRow(
                    id=job_id,
                    pipeline_name=pipeline_name,
                    pipeline_yaml=pipeline_yaml,
                    status="pending",
                    op_run_ids_json="[]",
                    output_artifact_ids_json="[]",
                    namespace=namespace,
                    submitted_at=_ensure_utc(submitted_at or datetime.now(UTC)),
                )
            )

    def update_job(
        self,
        *,
        job_id: str,
        status: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        op_run_ids: list[str] | None = None,
        output_artifact_ids: list[str] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self.session() as s:
            row = s.get(JobRow, job_id)
            if row is None:
                return
            if status is not None:
                row.status = status
            if started_at is not None:
                row.started_at = _ensure_utc(started_at)
            if finished_at is not None:
                row.finished_at = _ensure_utc(finished_at)
            if op_run_ids is not None:
                row.op_run_ids_json = json.dumps(op_run_ids)
            if output_artifact_ids is not None:
                row.output_artifact_ids_json = json.dumps(output_artifact_ids)
            if error is not None:
                row.error_json = json.dumps(error, sort_keys=True)

    def get_job(self, job_id: str, namespace: str = "default") -> Job | None:
        with self.session() as s:
            row = s.get(JobRow, job_id)
            if row is None or row.namespace != namespace:
                return None
            return _job_from_row(row)

    def list_jobs(
        self,
        *,
        status: str | None = None,
        namespace: str = "default",
        limit: int = 100,
    ) -> list[Job]:
        with self.session() as s:
            stmt = select(JobRow).where(JobRow.namespace == namespace)
            if status is not None:
                stmt = stmt.where(JobRow.status == status)
            stmt = stmt.order_by(JobRow.submitted_at.desc()).limit(limit)
            return [_job_from_row(r) for r in s.scalars(stmt).all()]

    def fail_orphaned_jobs(
        self,
        *,
        namespace: str | None = None,
        error_message: str = "process restarted while this job was running",
    ) -> list[str]:
        """Reset ``running``/``pending`` jobs to ``failed`` on startup.

        If the API process crashes mid-job, the row stays at
        ``running`` forever — there's no live task to flip it to
        ``completed``/``failed``. The API lifespan calls this at boot
        to sweep up any such rows that belong to the engine's
        namespace (so a multi-tenant deployment doesn't have one
        tenant's restart trip another tenant's in-flight job).
        Returns the ids that were reset.
        """
        from sqlalchemy import or_, update

        now = datetime.now(UTC)
        error_payload = json.dumps(
            {
                "error_class": "InterruptedRun",
                "message": error_message,
                "retryable": True,
            },
            sort_keys=True,
        )
        with self.session() as s:
            # Find the candidates first so we can return their ids;
            # the UPDATE then writes the new status atomically.
            stmt = select(JobRow.id).where(
                or_(JobRow.status == "running", JobRow.status == "pending")
            )
            if namespace is not None:
                stmt = stmt.where(JobRow.namespace == namespace)
            ids = list(s.scalars(stmt).all())
            if not ids:
                return []
            s.execute(
                update(JobRow)
                .where(JobRow.id.in_(ids))
                .values(
                    status="failed",
                    finished_at=now,
                    error_json=error_payload,
                )
            )
            return ids

    # ── api tokens (Phase 4 REST surface) ──

    def insert_api_token(
        self,
        *,
        token_id: str,
        token_hash: str,
        label: str,
        namespace: str = "default",
        created_at: datetime | None = None,
    ) -> None:
        with self.session() as s:
            s.add(
                ApiToken(
                    id=token_id,
                    token_hash=token_hash,
                    label=label,
                    namespace=namespace,
                    created_at=_ensure_utc(created_at or datetime.now(UTC)),
                )
            )

    def list_api_tokens(self, *, include_revoked: bool = False) -> list[ApiTokenInfo]:
        with self.session() as s:
            stmt = select(ApiToken)
            if not include_revoked:
                stmt = stmt.where(ApiToken.revoked_at.is_(None))
            stmt = stmt.order_by(ApiToken.created_at.desc())
            return [
                ApiTokenInfo(
                    id=r.id,
                    label=r.label,
                    namespace=r.namespace,
                    created_at=_ensure_utc(r.created_at),
                    revoked_at=(
                        _ensure_utc(r.revoked_at) if r.revoked_at else None
                    ),
                )
                for r in s.scalars(stmt).all()
            ]

    def find_api_token_by_hash(self, token_hash: str) -> ApiTokenInfo | None:
        with self.session() as s:
            stmt = select(ApiToken).where(ApiToken.token_hash == token_hash)
            row = s.scalars(stmt).first()
            if row is None or row.revoked_at is not None:
                return None
            return ApiTokenInfo(
                id=row.id,
                label=row.label,
                namespace=row.namespace,
                created_at=_ensure_utc(row.created_at),
                revoked_at=None,
            )

    def revoke_api_token(self, token_id: str) -> bool:
        with self.session() as s:
            row = s.get(ApiToken, token_id)
            if row is None or row.revoked_at is not None:
                return False
            row.revoked_at = datetime.now(UTC)
            return True

    def prune_events(
        self,
        *,
        older_than: datetime,
        namespace: str | None = None,
    ) -> int:
        """Delete events older than ``older_than``. Returns rows removed.

        ``namespace`` scopes the deletion. The Engine passes
        ``self.config.namespace`` at startup so a multi-tenant
        deployment (each tenant a separate process sharing the same
        cache.db) doesn't have tenant A's housekeeping nuke tenant B's
        events. Passing ``namespace=None`` (the cross-tenant admin
        path) still works for ``med`` operators who want to prune
        everything at once.
        """
        from sqlalchemy import delete

        with self.session() as s:
            stmt = delete(EventLogEntry).where(
                EventLogEntry.ts < _ensure_utc(older_than)
            )
            if namespace is not None:
                stmt = stmt.where(EventLogEntry.namespace == namespace)
            res = s.execute(stmt)
            return int(getattr(res, "rowcount", 0) or 0)

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
        truncated_reason: TruncatedReason | None = None
        if depth_remaining > 0:
            for pid in artifact.derived_from:
                child = self._build_lineage(pid, namespace, depth_remaining - 1, seen)
                if child is not None:
                    parents.append(child)
        elif artifact.derived_from:
            # Out of depth budget but the artifact still has parents we
            # haven't walked — flag the truncation so callers can render
            # it instead of silently flattening the tree.
            truncated_reason = "max_depth"
        return LineageNode(
            artifact=artifact,
            op_run=op_run,
            parents=parents,
            truncated_reason=truncated_reason,
        )

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
