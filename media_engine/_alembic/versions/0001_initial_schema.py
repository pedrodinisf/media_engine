"""Initial schema — cached_artifacts, cached_operation_runs, cost_log,
events, jobs, api_tokens.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-21

This single migration replicates the SQLAlchemy declarative schema from
``runtime/cache.py`` exactly. Subsequent schema changes ship as
additional migrations.

The DDL is dialect-neutral (text columns, indexes, unique constraints)
so the same migration runs on SQLite and Postgres. ``cache.Cache.__init__``
still calls ``Base.metadata.create_all`` for the SQLite fast path; on
Postgres deployments operators are expected to ``med db migrate``
instead (the cache then sees a populated schema and ``create_all`` is a
no-op).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "cached_artifacts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "derived_from_json", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column("produced_by", sa.String(), nullable=True),
        sa.Column("namespace", sa.String(), nullable=False, server_default="default"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "namespace", name="uq_artifact_id_namespace"),
    )
    op.create_index(
        "idx_artifacts_kind", "cached_artifacts", ["kind", "created_at"]
    )

    op.create_table(
        "cached_operation_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("op_name", sa.String(), nullable=False),
        sa.Column("op_version", sa.String(), nullable=False),
        sa.Column("backend_name", sa.String(), nullable=True),
        sa.Column("backend_version", sa.String(), nullable=True),
        sa.Column("params_hash", sa.String(), nullable=False),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("input_ids_json", sa.Text(), nullable=False),
        sa.Column("output_ids_json", sa.Text(), nullable=False),
        sa.Column("cost_estimate_json", sa.Text(), nullable=True),
        sa.Column("actual_cost_json", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("namespace", sa.String(), nullable=False, server_default="default"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "op_name",
            "op_version",
            "backend_name",
            "backend_version",
            "params_hash",
            "input_ids_json",
            "namespace",
            name="uq_operation_runs_lookup",
        ),
    )
    op.create_index(
        "idx_runs_lookup",
        "cached_operation_runs",
        [
            "op_name",
            "op_version",
            "backend_name",
            "backend_version",
            "params_hash",
            "input_ids_json",
        ],
    )

    op.create_table(
        "cost_log",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("op_name", sa.String(), nullable=False),
        sa.Column("backend_name", sa.String(), nullable=True),
        sa.Column("namespace", sa.String(), nullable=False, server_default="default"),
        sa.Column("estimated_cents", sa.Float(), server_default="0.0"),
        sa.Column("actual_cents", sa.Float(), server_default="0.0"),
        sa.Column("tokens_in", sa.Integer(), server_default="0"),
        sa.Column("tokens_out", sa.Integer(), server_default="0"),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
    )
    op.create_index("idx_cost_log_ts", "cost_log", ["ts"])
    op.create_index("idx_cost_log_op", "cost_log", ["op_name"])

    op.create_table(
        "events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("op_run_id", sa.String(), nullable=True),
        sa.Column("op_name", sa.String(), nullable=True),
        sa.Column("namespace", sa.String(), nullable=False, server_default="default"),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index("idx_events_ts", "events", ["ts"])
    op.create_index("idx_events_run", "events", ["op_run_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("pipeline_name", sa.String(), nullable=True),
        sa.Column("pipeline_yaml", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "op_run_ids_json", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "output_artifact_ids_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("namespace", sa.String(), nullable=False, server_default="default"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
    )
    op.create_index("idx_jobs_status", "jobs", ["status"])
    op.create_index("idx_jobs_submitted", "jobs", ["submitted_at"])

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("token_hash", sa.String(), nullable=False, unique=True),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("namespace", sa.String(), nullable=False, server_default="default"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_tokens_hash", "api_tokens", ["token_hash"])


def downgrade() -> None:
    op.drop_index("idx_tokens_hash", table_name="api_tokens")
    op.drop_table("api_tokens")
    op.drop_index("idx_jobs_submitted", table_name="jobs")
    op.drop_index("idx_jobs_status", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("idx_events_run", table_name="events")
    op.drop_index("idx_events_ts", table_name="events")
    op.drop_table("events")
    op.drop_index("idx_cost_log_op", table_name="cost_log")
    op.drop_index("idx_cost_log_ts", table_name="cost_log")
    op.drop_table("cost_log")
    op.drop_index("idx_runs_lookup", table_name="cached_operation_runs")
    op.drop_table("cached_operation_runs")
    op.drop_index("idx_artifacts_kind", table_name="cached_artifacts")
    op.drop_table("cached_artifacts")
