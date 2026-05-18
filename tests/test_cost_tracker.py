"""Cost ledger: Engine.run logging + CostTracker rollups."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from media_engine.ops import OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.runtime.cost_tracker import CostTracker, parse_since
from media_engine.runtime.engine import Engine


def _ctx(engine: Engine) -> OperationContext:
    return OperationContext(
        workdir=engine.storage.ensure_workdir("ct"),
        config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace,
    )


def test_parse_since_formats() -> None:
    assert parse_since("2026-05-01").tzinfo is UTC
    assert parse_since("2026-05-01T12:30:00").hour == 12
    with pytest.raises(ValueError, match="--since expects"):
        parse_since("not-a-date")


async def test_run_logs_one_row_then_cache_hit_does_not(
    engine: Engine, sample_mp4: Path
) -> None:
    op = AcquireUpload()
    [v] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                       _ctx(engine))
    engine.cache.upsert_artifact(v)

    [fs1] = await engine.run("video.sample_frames", inputs=[v.id], fps=2.0)
    tracker = CostTracker(engine.cache)
    assert tracker.summary().runs == 1

    # Cache hit on rerun → no new ledger row.
    [fs2] = await engine.run("video.sample_frames", inputs=[v.id], fps=2.0)
    assert fs1.id == fs2.id
    assert tracker.summary().runs == 1


async def test_summary_groups_by_op(
    engine: Engine, sample_mp4: Path
) -> None:
    op = AcquireUpload()
    [v] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                       _ctx(engine))
    engine.cache.upsert_artifact(v)
    await engine.run("video.sample_frames", inputs=[v.id], fps=1.0)
    await engine.run("video.sample_frames", inputs=[v.id], fps=4.0)

    summary = engine.cost_summary()
    assert summary.runs == 2
    by_op = {r.op_name: r for r in summary.by_op}
    assert by_op["video.sample_frames"].runs == 2


def test_record_and_filter(engine: Engine) -> None:
    now = datetime.now(UTC)
    engine.cache.record_cost(
        op_name="intelligence.extract", backend_name="gemini",
        estimated_cents=1.0, actual_cents=0.8, tokens_in=100,
        tokens_out=20, duration_seconds=0.5, ts=now,
    )
    engine.cache.record_cost(
        op_name="image.ocr", backend_name="rapidocr",
        estimated_cents=0.0, actual_cents=0.0, tokens_in=0,
        tokens_out=0, duration_seconds=1.0,
        ts=now - timedelta(days=10),
    )
    tracker = CostTracker(engine.cache)

    assert tracker.summary().runs == 2
    assert tracker.summary().actual_cents == pytest.approx(0.8)

    only_extract = tracker.summary(op_name="intelligence.extract")
    assert only_extract.runs == 1
    assert only_extract.tokens_in == 100

    recent = tracker.summary(since=now - timedelta(days=1))
    assert recent.runs == 1  # the 10-day-old row excluded

    rows = tracker.entries(limit=1)
    assert len(rows) == 1
    assert rows[0].op_name == "intelligence.extract"  # newest first
