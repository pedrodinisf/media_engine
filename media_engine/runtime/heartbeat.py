"""Per-`Engine.run` telemetry loop.

Wakes on a fixed interval and emits a `Progress(phase="heartbeat", ...)`
event carrying available RAM, the model-pool's running byte estimate,
and a running ETA derived from `op.cost_estimate(...).local_seconds`.

Hands the Web UI Logs/Jobs page enough signal to show:

  * a live "RAM free" gauge,
  * an ETA countdown,
  * a coarse fraction-complete (best-effort, derived from elapsed/eta).

Cheap (~0.1 ms per tick) and non-blocking — `EventBus.emit` drops the
oldest event on a full queue so a slow subscriber never wedges us.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from media_engine.runtime.events import Event, Progress

DEFAULT_INTERVAL_SECONDS = 2.0


async def heartbeat(
    *,
    emit: Callable[[Event], None],
    op_run_id: str,
    job_id: str | None,
    eta_seconds_initial: float,
    pool_bytes: Callable[[], int] | None = None,
    available_memory_gb: Callable[[], float] | None = None,
    interval: float = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Emit a Progress heartbeat every ``interval`` seconds until cancelled.

    ``eta_seconds_initial`` is the pre-run ``cost_estimate(...).local_seconds``;
    each tick we report ``max(eta - elapsed, 0)`` as the remaining ETA and
    ``min(elapsed / max(eta, 1e-6), 1.0)`` as the coarse fraction.

    ``pool_bytes`` / ``available_memory_gb`` are injected so tests can stub
    them without monkey-patching modules. Production callers pass
    ``ctx.model_pool.total_bytes_estimate`` and
    ``hardware.available_memory_gb``.

    Cancellation: the caller cancels + awaits this task in a ``finally``
    block. We absorb ``asyncio.CancelledError`` silently and re-raise so
    the parent's normal cleanup proceeds.
    """
    started_monotonic = time.monotonic()

    try:
        while True:
            await asyncio.sleep(interval)
            elapsed = time.monotonic() - started_monotonic
            remaining = max(eta_seconds_initial - elapsed, 0.0)
            # Fraction is best-effort. If the estimator returns 0 we can't
            # divide, so we keep it at 0 — the UI knows to interpret 0
            # heartbeat-fraction as "no ETA".
            fraction = (
                min(elapsed / eta_seconds_initial, 1.0)
                if eta_seconds_initial > 0.0
                else 0.0
            )

            try:
                ram_gb = available_memory_gb() if available_memory_gb else None
            except Exception:  # pragma: no cover -- best-effort telemetry
                ram_gb = None
            try:
                pool_b = pool_bytes() if pool_bytes else None
            except Exception:  # pragma: no cover -- best-effort telemetry
                pool_b = None

            emit(
                Progress(
                    event_id=uuid4().hex,
                    op_run_id=op_run_id,
                    job_id=job_id,
                    timestamp=datetime.now(UTC),
                    fraction=fraction,
                    message="",
                    phase="heartbeat",
                    available_memory_gb=ram_gb,
                    eta_seconds=remaining,
                    pool_bytes_estimate=pool_b,
                )
            )
    except asyncio.CancelledError:
        return
