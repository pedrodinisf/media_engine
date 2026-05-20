"""``med db`` — database migrations + SQLite-to-Postgres data move.

Two sub-commands:

- ``med db migrate`` — run alembic upgrade head against the cache URL
  the engine config resolves to. Idempotent.
- ``med db dump-sqlite-to-postgres --to <postgres-url>`` — one-shot
  copy of every row in the user's SQLite cache into a Postgres cache.
  Computes a SHA256 manifest pre- and post-copy and **refuses to
  delete the SQLite file** if the digests differ.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from media_engine.config import EngineConfig
from media_engine.runtime.cache import (
    ApiToken,
    Base,
    CachedArtifact,
    CachedOperationRun,
    CostLogEntry,
    EventLogEntry,
    JobRow,
)

app = typer.Typer(name="db", help="Database migrations + data movement.")
console = Console()
err_console = Console(stderr=True)


# Mapping of declarative classes → table-level digest fields used by the
# SHA256 manifest. Ordering is stable across runs (sorted by primary key)
# so the manifest is deterministic.
_TABLES: list[type[Any]] = [
    CachedArtifact,
    CachedOperationRun,
    CostLogEntry,
    EventLogEntry,
    JobRow,
    ApiToken,
]


def _alembic_config(db_url: str) -> Any:
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[2]
    ini_path = repo_root / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    return cfg


@app.command("migrate")
def cmd_migrate(
    db_url: Annotated[
        str | None,
        typer.Option(
            "--db-url",
            help="Override the cache URL alembic targets (default: engine config)",
        ),
    ] = None,
) -> None:
    """Run alembic upgrade head against the cache URL."""
    from alembic import command

    url = db_url or EngineConfig.load().resolve_cache_db_url()
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    console.print(f"[green]Migrated[/green] {url}")


@app.command("dump-sqlite-to-postgres")
def cmd_dump_sqlite_to_postgres(
    to_url: Annotated[
        str, typer.Option("--to", help="Postgres URL to copy into")
    ],
    from_url: Annotated[
        str | None,
        typer.Option(
            "--from", help="SQLite URL to copy from (default: engine config)"
        ),
    ] = None,
    delete_source: Annotated[
        bool,
        typer.Option(
            "--delete-source",
            help=(
                "Delete the SQLite source file after a successful copy. "
                "Refused if pre/post digests differ."
            ),
        ),
    ] = False,
) -> None:
    """Copy every cache row from SQLite to Postgres with a sha256 audit.

    Refuses to remove the source unless the pre/post digests match —
    the digest function is a deterministic hash over the row payloads
    keyed by primary key, so a clean copy round-trips to the same hash.
    """
    src_url = from_url or EngineConfig.load().resolve_cache_db_url()
    if not src_url.startswith("sqlite"):
        err_console.print(
            f"[red]--from must be a SQLite URL (got {src_url!r})[/red]"
        )
        raise typer.Exit(2)
    if to_url.startswith("sqlite"):
        err_console.print(
            f"[red]--to must be a Postgres URL (got {to_url!r})[/red]"
        )
        raise typer.Exit(2)

    src_engine = create_engine(src_url, future=True)
    dst_engine = create_engine(to_url, future=True)
    # Ensure the destination schema exists; idempotent.
    Base.metadata.create_all(dst_engine)

    src_session = sessionmaker(bind=src_engine, expire_on_commit=False)
    dst_session = sessionmaker(bind=dst_engine, expire_on_commit=False)

    pre_digest = _digest(src_session)
    console.print(f"Source digest:      [cyan]{pre_digest}[/cyan]")

    _copy_all(src_session, dst_session)

    post_digest = _digest(dst_session)
    console.print(f"Destination digest: [cyan]{post_digest}[/cyan]")
    if pre_digest != post_digest:
        err_console.print(
            "[red]Pre/post digest mismatch — refusing to touch the source.[/red]"
        )
        raise typer.Exit(1)
    console.print("[green]Copy verified.[/green]")

    if delete_source:
        # SQLite URLs are sqlite+driver:///<path>
        prefix, _, path = src_url.partition(":///")
        del prefix
        sqlite_path = Path(path)
        if sqlite_path.exists():
            sqlite_path.unlink()
            console.print(f"[green]Removed[/green] {sqlite_path}")


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _digest(session_factory: Any) -> str:
    """SHA256 over deterministically-ordered row payloads.

    Independent of insertion order — sorts by each table's primary key
    before hashing — so a clean copy of every row reproduces the same
    digest on either side.
    """
    h = hashlib.sha256()
    with session_factory() as s:
        # Skip tables that don't exist yet (alembic may not have created
        # them on the destination); inspect introspects the real schema.
        insp = inspect(s.connection())
        existing = set(insp.get_table_names())
        for table_cls in _TABLES:
            tname = table_cls.__tablename__
            if tname not in existing:
                continue
            stmt = select(table_cls).order_by(*_pk_cols(table_cls))
            for row in s.scalars(stmt).all():
                h.update(tname.encode())
                h.update(b"\x00")
                payload = _row_payload(row)
                h.update(
                    json.dumps(payload, sort_keys=True, default=str).encode(
                        "utf-8"
                    )
                )
                h.update(b"\x00")
    return h.hexdigest()


def _pk_cols(table_cls: Any) -> list[Any]:
    return [c for c in table_cls.__table__.columns if c.primary_key]


def _row_payload(row: Any) -> dict[str, Any]:
    cols = [c.name for c in row.__table__.columns]
    return {c: getattr(row, c) for c in cols}


def _copy_all(src_session_factory: Any, dst_session_factory: Any) -> None:
    """Bulk copy every row of every tracked table.

    Truncates the destination table first (idempotent re-run) so a
    partial earlier copy doesn't poison the digest.
    """
    for table_cls in _TABLES:
        with dst_session_factory() as ds:
            ds.execute(table_cls.__table__.delete())
        with src_session_factory() as ss:
            rows = list(ss.scalars(select(table_cls)).all())
        if not rows:
            continue
        # Detach + rebuild as a plain dict for re-insertion. SQLAlchemy
        # ORM objects are bound to their source session; passing them
        # straight to a different session triggers state errors.
        payloads = [_row_payload(r) for r in rows]
        with dst_session_factory() as ds:
            ds.execute(table_cls.__table__.insert(), payloads)
            ds.commit()
