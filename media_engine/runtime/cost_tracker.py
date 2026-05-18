"""Spend reporting over the append-only ``cost_log`` ledger.

``Engine.run`` records one ledger row per *actual* execution (never on a
cache hit). This module turns that ledger into the numbers ``med cost``
shows: a per-op rollup and a recent-runs list. No domain opinions — just
sums and counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from media_engine.runtime.cache import Cache, CostLogEntry

__all__ = ["CostSummary", "CostTracker", "OpRollup", "parse_since"]


def parse_since(value: str) -> datetime:
    """Parse a ``YYYY-MM-DD`` (or full ISO) ``--since`` value as UTC."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(
            f"--since expects YYYY-MM-DD or ISO-8601, got {value!r}"
        ) from e
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


@dataclass(frozen=True)
class OpRollup:
    op_name: str
    runs: int
    estimated_cents: float
    actual_cents: float
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class CostSummary:
    runs: int
    estimated_cents: float
    actual_cents: float
    tokens_in: int
    tokens_out: int
    by_op: list[OpRollup]


class CostTracker:
    """Read-only views over the cost ledger."""

    def __init__(self, cache: Cache) -> None:
        self._cache = cache

    def entries(
        self,
        *,
        since: datetime | None = None,
        op_name: str | None = None,
        namespace: str | None = None,
        limit: int | None = None,
    ) -> list[CostLogEntry]:
        return self._cache.cost_log(
            since=since, op_name=op_name, namespace=namespace, limit=limit
        )

    def summary(
        self,
        *,
        since: datetime | None = None,
        op_name: str | None = None,
        namespace: str | None = None,
    ) -> CostSummary:
        rows = self._cache.cost_log(
            since=since, op_name=op_name, namespace=namespace
        )
        per_op: dict[str, list[CostLogEntry]] = {}
        for r in rows:
            per_op.setdefault(r.op_name, []).append(r)

        by_op: list[OpRollup] = []
        for name in sorted(per_op):
            grp = per_op[name]
            by_op.append(
                OpRollup(
                    op_name=name,
                    runs=len(grp),
                    estimated_cents=round(
                        sum(x.estimated_cents for x in grp), 4
                    ),
                    actual_cents=round(sum(x.actual_cents for x in grp), 4),
                    tokens_in=sum(x.tokens_in for x in grp),
                    tokens_out=sum(x.tokens_out for x in grp),
                )
            )
        return CostSummary(
            runs=len(rows),
            estimated_cents=round(sum(x.estimated_cents for x in rows), 4),
            actual_cents=round(sum(x.actual_cents for x in rows), 4),
            tokens_in=sum(x.tokens_in for x in rows),
            tokens_out=sum(x.tokens_out for x in rows),
            by_op=by_op,
        )
