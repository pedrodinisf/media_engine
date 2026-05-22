"""Phase 6 commit 46 — cost-ledger REST surface.

The Web UI's ``/ui/cost`` panel surfaces the same numbers ``med cost
summary`` and ``med cost ls`` print: per-op rollups + the recent run
log. The shell + UI share the same engine APIs (``Engine.cost_summary``
+ ``Engine.cost_log_entries``); these routes thin-wrap them with
group-by + pagination handled in-route per plan §3.5.

Both endpoints are bearer-gated and scope reads to ``token.namespace``.
The ``until`` query param + ``group_by`` aggregation are route-level
concerns (the engine APIs themselves don't support them); the engine
contract from Phase 5 stays intact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from media_engine.api._state import AppState
from media_engine.api.routes import get_state, require_token
from media_engine.runtime.cache import ApiTokenInfo, CostLogEntry

router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────


class CostRollupRow(BaseModel):
    key: str
    count: int
    total_cents: float
    total_usd: float
    tokens_in: int
    tokens_out: int


class CostSummaryResponse(BaseModel):
    rows: list[CostRollupRow]
    total_cents: float
    group_by: Literal["op", "backend", "namespace"]
    since: str | None
    until: str | None


class CostLogItem(BaseModel):
    """Plain-dict mirror of ``CostLogEntry`` for over-the-wire."""

    id: str
    ts: str
    op_name: str
    backend_name: str | None
    namespace: str
    estimated_cents: float
    actual_cents: float
    tokens_in: int
    tokens_out: int
    duration_seconds: float | None


class CostLogResponse(BaseModel):
    items: list[CostLogItem]
    next_offset: int | None = None
    limit: int
    offset: int


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _parse_iso(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail=f"bad ISO timestamp for {field}: {e}"
        ) from None
    # SQLite returns offset-naive datetimes; normalize the request value
    # to naive UTC so direct ``row.ts <= until_dt`` comparisons work
    # against the engine's stored rows.
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _row_to_dict(row: CostLogEntry) -> CostLogItem:
    """SQLAlchemy ORM → dict. We hand-list fields because the ORM model
    has no ``.model_dump()`` and pyright catches accidental ``getattr``
    drift (commit 43 audit)."""
    return CostLogItem(
        id=row.id,
        ts=row.ts.isoformat(),
        op_name=row.op_name,
        backend_name=row.backend_name,
        namespace=row.namespace,
        estimated_cents=float(row.estimated_cents),
        actual_cents=float(row.actual_cents),
        tokens_in=int(row.tokens_in),
        tokens_out=int(row.tokens_out),
        duration_seconds=(
            float(row.duration_seconds)
            if row.duration_seconds is not None
            else None
        ),
    )


def _group_key(row: CostLogEntry, group_by: str) -> str:
    if group_by == "op":
        return row.op_name
    if group_by == "backend":
        return row.backend_name or "(none)"
    return row.namespace


# ─────────────────────────────────────────────────────────────────
# GET /cost/summary
# ─────────────────────────────────────────────────────────────────


@router.get("/cost/summary", response_model=CostSummaryResponse)
def get_cost_summary(
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
    since: Annotated[str | None, Query(description="ISO-8601 lower bound")] = None,
    until: Annotated[str | None, Query(description="ISO-8601 upper bound")] = None,
    group_by: Annotated[
        Literal["op", "backend", "namespace"], Query()
    ] = "op",
) -> CostSummaryResponse:
    """Per-key spend rollup over the cost ledger.

    ``group_by=op`` is what ``med cost summary`` shows; the UI also
    surfaces ``backend`` and ``namespace`` for cross-cut views. The
    engine's ``CostTracker.summary`` only groups by op, so we fetch
    rows once and aggregate in-route for the other keys — keeping the
    engine contract narrow per plan §3.5.
    """
    del token  # required for auth gate; cost is namespace-scoped via Engine
    since_dt = _parse_iso(since, "since")
    until_dt = _parse_iso(until, "until")

    rows = state.engine.cost_log_entries(since=since_dt)
    if until_dt is not None:
        # cost_log_entries doesn't accept an upper bound; filter here.
        rows = [r for r in rows if r.ts <= until_dt]

    buckets: dict[str, dict[str, float]] = {}
    for r in rows:
        key = _group_key(r, group_by)
        b = buckets.setdefault(
            key,
            {
                "count": 0.0,
                "total_cents": 0.0,
                "tokens_in": 0.0,
                "tokens_out": 0.0,
            },
        )
        b["count"] += 1
        b["total_cents"] += float(r.actual_cents)
        b["tokens_in"] += float(r.tokens_in)
        b["tokens_out"] += float(r.tokens_out)

    rollup: list[CostRollupRow] = []
    for key in sorted(buckets):
        b = buckets[key]
        cents = round(b["total_cents"], 4)
        rollup.append(
            CostRollupRow(
                key=key,
                count=int(b["count"]),
                total_cents=cents,
                total_usd=round(cents / 100.0, 6),
                tokens_in=int(b["tokens_in"]),
                tokens_out=int(b["tokens_out"]),
            )
        )

    grand_total = round(sum(r.total_cents for r in rollup), 4)
    return CostSummaryResponse(
        rows=rollup,
        total_cents=grand_total,
        group_by=group_by,
        since=since,
        until=until,
    )


# ─────────────────────────────────────────────────────────────────
# GET /cost/log
# ─────────────────────────────────────────────────────────────────


@router.get("/cost/log", response_model=CostLogResponse)
def get_cost_log(
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
    since: Annotated[str | None, Query(description="ISO-8601 lower bound")] = None,
    until: Annotated[str | None, Query(description="ISO-8601 upper bound")] = None,
    op_name: Annotated[str | None, Query(alias="op")] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CostLogResponse:
    """Paginated cost-log rows, newest first.

    Offset-based pagination so the UI's drill-down table can walk a
    long ledger. We over-fetch by one row to detect a next page
    without a separate COUNT query.
    """
    del token  # auth-only; namespace scoping happens inside cost_log_entries
    since_dt = _parse_iso(since, "since")
    until_dt = _parse_iso(until, "until")

    # cost_log_entries returns newest-first; over-fetch by offset+limit+1
    # so we can both apply offset/until filters in Python AND detect a
    # next page. For tiny ledgers this is fine; if anyone hits a regime
    # where this matters, push pagination + ``until`` into the cache.
    raw = state.engine.cost_log_entries(
        since=since_dt,
        op_name=op_name,
        limit=offset + limit + 1,
    )
    if until_dt is not None:
        raw = [r for r in raw if r.ts <= until_dt]

    window = raw[offset : offset + limit + 1]
    has_more = len(window) > limit
    page = window[:limit]
    items = [_row_to_dict(r) for r in page]
    return CostLogResponse(
        items=items,
        next_offset=(offset + limit) if has_more else None,
        limit=limit,
        offset=offset,
    )
