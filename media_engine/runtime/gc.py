"""Workdir garbage collection.

Per-job workdirs (``{workdir}/{job_id}/``) are cleaned up by
``Engine.run`` in its ``finally`` block on the happy path, but a process
crash, a SIGKILL'd daemon, or a long-running pipeline that aborted
mid-flight leave residue behind. ``runtime.gc`` provides:

- ``sweep_workdirs(workdir, *, retention)`` — drop directories older
  than ``retention`` (default 24 h). Called by the daemon on a timer
  and by ``med storage gc``.
- ``periodic_workdir_gc(...)`` — async loop the daemon spawns at
  startup.

The retention window is the only knob; the gc does not look at job
status (the cache rows do that). A subdirectory whose mtime is younger
than the window is kept regardless of whether the job is running —
that's the simple invariant we promise.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from datetime import timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def sweep_workdirs(
    workdir: Path, *, retention: timedelta = timedelta(hours=24)
) -> list[Path]:
    """Remove every workdir subdir older than ``retention``.

    Returns the paths that were removed (informational; the daemon
    just logs the count).
    """
    if not workdir.exists():
        return []
    cutoff = time.time() - retention.total_seconds()
    removed: list[Path] = []
    for entry in workdir.iterdir():
        if not entry.is_dir():
            continue
        try:
            stat = entry.stat()
        except FileNotFoundError:
            continue
        if stat.st_mtime > cutoff:
            continue
        # Don't follow into a workdir whose contents are actively being
        # streamed into; honor an mtime touch on the directory itself.
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry)
    return removed


async def periodic_workdir_gc(
    workdir: Path,
    *,
    interval: timedelta,
    retention: timedelta = timedelta(hours=24),
) -> None:
    """Run ``sweep_workdirs`` forever, sleeping ``interval`` between sweeps.

    The daemon spawns this as a background task at startup. Cancellation
    is the only way out. Transient errors (a permission-denied entry,
    a file that vanished mid-sweep) are caught + logged at WARNING so
    the loop keeps running — but they no longer disappear silently the
    way a bare ``suppress(Exception)`` did, so operators have visibility
    when GC starts failing systematically.
    """
    while True:
        try:
            sweep_workdirs(workdir, retention=retention)
        except Exception:
            logger.warning(
                "periodic_workdir_gc sweep failed; retrying after interval",
                exc_info=True,
            )
        await asyncio.sleep(interval.total_seconds())


def gc_interval_from_env(default_seconds: int = 3600) -> int:
    """Read ``MEDIA_ENGINE_GC_INTERVAL`` (seconds) with a sane default."""
    raw = os.environ.get("MEDIA_ENGINE_GC_INTERVAL")
    if raw is None:
        return default_seconds
    try:
        v = int(raw)
        return v if v > 0 else default_seconds
    except ValueError:
        return default_seconds
