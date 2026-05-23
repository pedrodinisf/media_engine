"""Add ``events.job_id`` column for SSE replay (B-001).

Revision ID: 0002_events_job_id
Revises: 0001_initial_schema
Create Date: 2026-05-23

Adds a nullable ``job_id`` column + index to the ``events`` table.
Existing rows have ``job_id = NULL`` (historical events from before
the fix have no job correlation; the SSE pumper just won't replay
them).

The schema change is additive and SQLite/Postgres-compatible.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_events_job_id"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("job_id", sa.String(), nullable=True))
    op.create_index("idx_events_job", "events", ["job_id"])


def downgrade() -> None:
    op.drop_index("idx_events_job", table_name="events")
    with op.batch_alter_table("events") as batch:
        batch.drop_column("job_id")
