"""End-to-end integration: heartbeat + log_pump fire during `Engine.run`.

Cheap synthetic op that:

1. spawns `python -c 'print(...)'` via `attach_subprocess`
2. sleeps long enough to let the engine's heartbeat task tick at least once

We sink the engine's event bus, then assert:

* at least one `LogLine(source="echo-test", line="hello observability")`
  reached the bus (proves Phase A.3 log pumps wire through Engine.run)
* at least one `Progress(phase="heartbeat", available_memory_gb≠None)`
  reached the bus (proves Phase A.2 heartbeat task wired into Engine.run)
"""

from __future__ import annotations

import asyncio
import time

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.runtime.engine import Engine
from media_engine.runtime.events import Event, LogLine, Progress
from media_engine.runtime.heartbeat import DEFAULT_INTERVAL_SECONDS
from media_engine.runtime.log_pump import attach_subprocess


class _ObservabilityParams(BaseModel):
    # Sleep just long enough to guarantee one heartbeat tick.
    sleep_seconds: float = DEFAULT_INTERVAL_SECONDS + 0.3


@register_op
class _ObservabilityProbeOp(Operation):
    """Test-only op that emits one LogLine and sleeps past one heartbeat."""

    name = "test.observability_probe"
    version = "1.0.0"
    input_kinds = ()
    output_kinds = ()
    params_model = _ObservabilityParams
    records_cost = False

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, _ObservabilityParams)
        proc = await asyncio.create_subprocess_exec(
            "python",
            "-c",
            "print('hello observability')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        handle = attach_subprocess(
            proc,
            source="echo-test",
            emit=ctx.emit,
            op_run_id="probe",
        )
        try:
            await proc.wait()
        finally:
            await handle.aclose()
        # Hold the run open long enough for the heartbeat task to tick.
        await asyncio.sleep(params.sleep_seconds)
        return []

    def cost_estimate(self, inputs, params):
        # ETA needs to be > 0 so the heartbeat fraction is meaningful.
        return CostEstimate(local_seconds=2.0)


async def test_engine_run_emits_heartbeat_and_loglines(engine: Engine) -> None:
    captured: list[Event] = []
    engine.event_bus.add_sink(captured.append)

    start = time.monotonic()
    await engine.run("test.observability_probe", inputs=[])
    elapsed = time.monotonic() - start
    # Sanity: the op slept past one heartbeat tick.
    assert elapsed >= DEFAULT_INTERVAL_SECONDS

    loglines = [
        e for e in captured
        if isinstance(e, LogLine) and e.source == "echo-test"
    ]
    assert any(e.line == "hello observability" for e in loglines), (
        f"expected one LogLine from echo-test, got {[(e.source, e.line) for e in loglines]}"
    )

    heartbeats = [
        e for e in captured
        if isinstance(e, Progress) and e.phase == "heartbeat"
    ]
    assert heartbeats, "expected at least one heartbeat Progress event"
    # Heartbeat must populate at least one of the new optional fields.
    assert any(
        hb.available_memory_gb is not None or hb.pool_bytes_estimate is not None
        for hb in heartbeats
    ), "heartbeat events should carry RAM / pool estimates"
