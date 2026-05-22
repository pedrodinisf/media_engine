"""Background job runner — wraps ``Engine.run`` / ``Engine.run_pipeline``.

The REST surface is async-first: ``POST /run`` and ``POST /pipelines``
both return a ``job_id`` immediately while the actual work is scheduled
on the asyncio event loop. This module owns the bookkeeping that lets
``GET /jobs/{id}`` and SSE event streams stay accurate: row inserts,
status transitions, op-run id collection, error capture.

There is no separate worker pool — uvicorn's single event loop runs the
engine + the API + the job tasks. Concurrency is bounded by the same
resource semaphores the CLI uses (``apple_neural_engine: 1``,
``cloud_concurrent: 8``).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from media_engine.api._state import AppState
from media_engine.runtime.dag import DAGResult, Pipeline
from media_engine.runtime.events import build_op_failed
from media_engine.runtime.lineage import OperationRunRef


def _classify_error(exc: BaseException, *, job_id: str) -> dict[str, Any]:
    """Wrap an exception into the same OpFailed envelope the engine uses.

    ``op_run_id`` carries the REST job_id so the persisted error trail
    can be correlated against the cache row even though there's no
    ``cached_operation_runs`` row when the failure is at the
    submission/wrapper level.
    """
    envelope = build_op_failed(exc, op_run_id=job_id, job_id=job_id)
    return {
        "error_class": envelope.error_class,
        "message": envelope.message,
        "retryable": envelope.retryable,
        "suggested_action": envelope.suggested_action,
        "traceback": envelope.traceback,
    }


def submit_run_op(
    state: AppState,
    *,
    op_name: str,
    inputs: list[str],
    backend: str | None,
    params: dict[str, Any],
    namespace: str,
) -> str:
    """Insert a pending job row + schedule the background runner. Returns the id."""
    job_id = _new_job_id()
    state.cache.insert_job(
        job_id=job_id,
        pipeline_name=None,
        pipeline_yaml=None,
        namespace=namespace,
    )
    task = asyncio.create_task(
        _run_single_op(state, job_id, op_name, inputs, backend, params)
    )
    state.track_job(job_id, task)
    return job_id


def submit_pipeline(
    state: AppState,
    *,
    pipeline: Pipeline,
    namespace: str,
    pipeline_name: str | None = None,
    pipeline_yaml: str | None = None,
) -> str:
    job_id = _new_job_id()
    state.cache.insert_job(
        job_id=job_id,
        pipeline_name=pipeline_name or pipeline.name,
        pipeline_yaml=pipeline_yaml,
        namespace=namespace,
    )
    task = asyncio.create_task(_run_pipeline(state, job_id, pipeline))
    state.track_job(job_id, task)
    return job_id


async def cancel_job(state: AppState, job_id: str) -> bool:
    """Cancel a running job's task. Returns True if a task was found.

    Race-safe: if the task completed (or failed) during our await, the
    runner's own ``finally`` block has already written a terminal status,
    and we must not overwrite it with ``cancelled``. We check
    ``task.cancelled()`` *after* awaiting and only flip the cache row
    when cancellation actually took effect.
    """
    task = state.pop_job_task(job_id)
    if task is None:
        return False
    was_done = task.done()
    if not was_done:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    # If the task raced to natural completion before / during our
    # cancel, ``task.cancelled()`` is False — the runner already wrote
    # ``completed`` / ``failed``. Honor that terminal status and signal
    # "not actually cancelled" to the caller.
    if was_done or not task.cancelled():
        return False
    state.cache.update_job(
        job_id=job_id,
        status="cancelled",
        finished_at=datetime.now(UTC),
    )
    return True


# ─────────────────────────────────────────────────────────────────
# Background runners
# ─────────────────────────────────────────────────────────────────


async def _run_single_op(
    state: AppState,
    job_id: str,
    op_name: str,
    inputs: list[str],
    backend: str | None,
    params: dict[str, Any],
) -> None:
    state.cache.update_job(
        job_id=job_id, status="running", started_at=datetime.now(UTC)
    )
    try:
        artifacts = await state.engine.run(
            op_name, inputs=inputs, backend=backend, **params
        )
    except asyncio.CancelledError:
        # `cancel_job` already wrote the cancelled row; just exit quietly.
        raise
    except BaseException as exc:  # noqa: BLE001
        state.cache.update_job(
            job_id=job_id,
            status="failed",
            finished_at=datetime.now(UTC),
            error=_classify_error(exc, job_id=job_id),
        )
        return
    finally:
        state.untrack_job(job_id)
    op_run_ids = [a.produced_by for a in artifacts if a.produced_by]
    state.cache.update_job(
        job_id=job_id,
        status="completed",
        finished_at=datetime.now(UTC),
        op_run_ids=op_run_ids,
        output_artifact_ids=[a.id for a in artifacts],
    )


async def _run_pipeline(
    state: AppState, job_id: str, pipeline: Pipeline
) -> None:
    state.cache.update_job(
        job_id=job_id, status="running", started_at=datetime.now(UTC)
    )
    try:
        result: DAGResult = await state.engine.run_pipeline(pipeline)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001
        state.cache.update_job(
            job_id=job_id,
            status="failed",
            finished_at=datetime.now(UTC),
            error=_classify_error(exc, job_id=job_id),
        )
        return
    finally:
        state.untrack_job(job_id)

    op_run_ids: list[str] = []
    output_ids: list[str] = []
    for node_id, success in result.successes.items():
        del node_id
        for art in success.artifacts:
            output_ids.append(art.id)
            if art.produced_by:
                op_run_ids.append(art.produced_by)

    if result.failures:
        first_failed = next(iter(result.failures.values()))
        state.cache.update_job(
            job_id=job_id,
            status="failed",
            finished_at=datetime.now(UTC),
            op_run_ids=op_run_ids,
            output_artifact_ids=output_ids,
            error={
                "error_class": first_failed.error_class,
                "message": first_failed.message,
                "failed_node_id": first_failed.node_id,
                "failed_dependency": first_failed.failed_dependency,
            },
        )
        return

    state.cache.update_job(
        job_id=job_id,
        status="completed",
        finished_at=datetime.now(UTC),
        op_run_ids=op_run_ids,
        output_artifact_ids=output_ids,
    )


def operation_runs_for_job(
    state: AppState, op_run_ids: list[str]
) -> list[OperationRunRef]:
    """Hydrate the op-run ids stored on a job into ``OperationRunRef``s.

    Used by ``GET /jobs/{id}`` so clients can see which ops ran without
    a second round-trip per id.
    """
    out: list[OperationRunRef] = []
    for run_id in op_run_ids:
        ref = state.cache.get_run(run_id)
        if ref is not None:
            out.append(ref)
    return out


def _new_job_id() -> str:
    from uuid import uuid4

    return uuid4().hex
