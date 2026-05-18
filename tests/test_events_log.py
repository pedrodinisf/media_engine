"""OpFailed envelope + durable event tail (Engine.run → cost.db events)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from media_engine.runtime.engine import Engine


async def test_success_persists_started_and_completed(
    engine: Engine, sample_mp4: Path
) -> None:
    await engine.run("acquire.upload", source_path=sample_mp4)
    types = [e.type for e in engine.event_log_entries()]
    assert "op_started" in types
    assert "op_completed" in types


async def test_failure_persists_structured_op_failed(
    engine: Engine, tmp_path: Path
) -> None:
    missing = tmp_path / "nope.mp4"
    with pytest.raises(FileNotFoundError):
        await engine.run("acquire.upload", source_path=missing)

    rows = engine.event_log_entries()
    failed = [e for e in rows if e.type == "op_failed"]
    assert len(failed) == 1
    payload = json.loads(failed[0].payload_json)
    assert payload["error_class"] == "FileNotFoundError"
    assert payload["retryable"] is False
    assert "Deterministic error" in payload["suggested_action"]
    assert payload["traceback"]  # non-empty

    # OpStarted shares the op_run_id with the failure.
    started = [e for e in rows if e.type == "op_started"]
    assert started and started[0].op_run_id == failed[0].op_run_id


async def test_event_log_filter_by_op_run_id(
    engine: Engine, sample_mp4: Path
) -> None:
    await engine.run("acquire.upload", source_path=sample_mp4)
    rows = engine.event_log_entries()
    run_id = rows[0].op_run_id
    assert run_id is not None
    same = engine.event_log_entries(op_run_id=run_id)
    assert same and all(e.op_run_id == run_id for e in same)


def test_prune_events_drops_old_rows(engine: Engine) -> None:
    old = datetime.now(UTC) - timedelta(days=30)
    engine.cache.record_event(
        ts=old, event_type="op_started", op_run_id="r1",
        op_name="x.y", payload_json="{}",
    )
    engine.cache.record_event(
        ts=datetime.now(UTC), event_type="op_started", op_run_id="r2",
        op_name="x.y", payload_json="{}",
    )
    removed = engine.cache.prune_events(
        older_than=datetime.now(UTC) - timedelta(days=7)
    )
    assert removed == 1
    remaining = engine.cache.event_log()
    assert [e.op_run_id for e in remaining] == ["r2"]
