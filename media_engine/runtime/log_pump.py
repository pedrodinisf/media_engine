"""Bridges subprocess stdout/stderr and Python `logging` → `LogLine` events.

`LogLine` has been on the event bus since Phase 0 but no producer ever
emitted one. Phase A.3 fills that gap so the Web UI Logs tab gets real
output from the long-running native backends (ffmpeg, vllm-mlx server,
mlx-whisper, pyannote) plus any third-party library that goes through
the stdlib `logging` module.

Two surfaces, each minimal:

* `attach_subprocess(proc, source=…, emit=…, …)` spawns two background
  tasks (stdout + stderr) that read line-by-line and emit one `LogLine`
  per line. Returns a handle that the caller awaits in their `finally`.
* `attach_logger(logger_name, source=…, emit=…, …)` installs a
  `logging.Handler` and returns a removal token; the caller MUST call
  `token.detach()` in `finally` so the handler doesn't leak across runs.

Both surfaces honour:

* line-level dedup — collapses consecutive identical lines (the common
  case for tqdm-style `\r` traffic that surfaces as repeated whole lines
  after CR-stripping).
* a hard cap of `MAX_LINES_PER_RUN` (5000 by default); on overflow we
  emit one final `LogLine(level="warn", line="log truncated …")` and
  stop emitting from that source. Prevents a runaway backend from
  flooding the SSE queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from media_engine.runtime.events import Event, LogLine

MAX_LINES_PER_RUN = 5000


@dataclass
class LinePump:
    """Tracks per-source dedup + truncation state shared by stream pumps.

    Exposed publicly so backends that already iterate stdout/stderr inline
    (e.g. `ops/video/extract_audio.py`, which parses ffmpeg `time=` for
    Progress) can push the same lines as `LogLine` events without a
    second subprocess.
    """

    source: str
    emit: Callable[[Event], None]
    op_run_id: str
    job_id: str | None
    count: int = 0
    last_line: str = ""
    truncated: bool = False

    def push(self, level: str, line: str) -> None:
        if self.truncated:
            return
        # Strip trailing CR/LF noise; tqdm's '\r' rewrites surface as
        # the same string repeated, which the dedup below collapses.
        clean = line.rstrip("\r\n")
        if not clean:
            return
        if clean == self.last_line:
            return
        self.last_line = clean
        if self.count >= MAX_LINES_PER_RUN:
            self.truncated = True
            self.emit(
                LogLine(
                    event_id=uuid4().hex,
                    op_run_id=self.op_run_id,
                    job_id=self.job_id,
                    timestamp=datetime.now(UTC),
                    level="warn",
                    source=self.source,
                    line=f"log truncated past {MAX_LINES_PER_RUN} lines",
                )
            )
            return
        self.count += 1
        self.emit(
            LogLine(
                event_id=uuid4().hex,
                op_run_id=self.op_run_id,
                job_id=self.job_id,
                timestamp=datetime.now(UTC),
                level=level,
                source=self.source,
                line=clean,
            )
        )


async def _drain_stream(
    stream: asyncio.StreamReader | None,
    *,
    pump: LinePump,
    level: str,
) -> None:
    if stream is None:
        return
    while True:
        try:
            raw = await stream.readline()
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception:  # pragma: no cover -- best-effort capture
            return
        if not raw:
            return
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover -- decode is forgiving
            continue
        # readline() returns one line per call; if the producer wrote
        # CR-only updates (tqdm), the OS pipe buffers until newline so
        # we end up with longer combined chunks. Split on \r so we still
        # see the latest tqdm frame as its own line.
        for piece in text.split("\r"):
            pump.push(level, piece)


@dataclass
class SubprocessLogHandle:
    """Awaitable handle returned by `attach_subprocess` / `attach_file_tail`."""

    tasks: list[asyncio.Task[None]]

    def cancel(self) -> None:
        """Cancel the underlying pump tasks.

        Subprocess pumps end naturally on stream EOF; only file-tail pumps
        (no natural end) need an explicit cancel before `aclose()`.
        """
        for t in self.tasks:
            if not t.done():
                t.cancel()

    async def aclose(self) -> None:
        """Wait for the stream pumps to drain. Call in `finally`."""
        if not self.tasks:
            return
        # gather with return_exceptions so a CancelledError on one pump
        # doesn't swallow the other.
        await asyncio.gather(*self.tasks, return_exceptions=True)


def attach_subprocess(
    proc: asyncio.subprocess.Process,
    *,
    source: str,
    emit: Callable[[Event], None],
    op_run_id: str,
    job_id: str | None = None,
    stdout_level: str = "info",
    stderr_level: str = "info",
) -> SubprocessLogHandle:
    """Spawn stdout + stderr pumps that emit one `LogLine` per line.

    `source` labels the events (e.g. ``"ffmpeg"``, ``"vllm-mlx"``); the
    Web UI uses it for the per-source filter dropdown.

    `stderr_level` defaults to ``"info"`` because well-behaved Unix tools
    (ffmpeg, vllm) write progress to stderr; flagging every line as a
    warning would be noise. Callers can override per-backend.
    """
    pump = LinePump(
        source=source, emit=emit, op_run_id=op_run_id, job_id=job_id
    )
    tasks: list[asyncio.Task[None]] = []
    if proc.stdout is not None:
        tasks.append(
            asyncio.create_task(
                _drain_stream(proc.stdout, pump=pump, level=stdout_level)
            )
        )
    if proc.stderr is not None:
        tasks.append(
            asyncio.create_task(
                _drain_stream(proc.stderr, pump=pump, level=stderr_level)
            )
        )
    return SubprocessLogHandle(tasks=tasks)


async def _tail_file(
    path: str,
    *,
    pump: LinePump,
    level: str,
    poll_interval: float,
) -> None:
    """Tail a growing log file from the current end, pushing new lines.

    Handles two real-world cases beyond the happy path:

    * **File is replaced** (vllm-mlx server stop+restart truncates / rotates
      the log file). We detect this by comparing the current size against
      our last known offset; if it shrank, we reset to 0 and re-read from
      the new beginning.
    * **File doesn't exist yet**. We poll quietly until it appears, then
      start tailing from byte 0 so the operator sees the very first lines.
    """
    from pathlib import Path

    p = Path(path)
    # Start at current EOF — we don't want to replay history; the caller
    # opted-in for *new* output from when they attached.
    try:
        offset = p.stat().st_size if p.exists() else 0
    except OSError:
        offset = 0
    leftover = ""
    while True:
        await asyncio.sleep(poll_interval)
        if not p.exists():
            # File was deleted (or hasn't been created yet); reset state
            # so we start fresh when it (re)appears.
            offset = 0
            leftover = ""
            continue
        try:
            current_size = p.stat().st_size
        except OSError:
            continue
        # Truncation / log rotation: size shrank below our last offset.
        # Reset to 0 so we pick up the new content from the start.
        if current_size < offset:
            offset = 0
            leftover = ""
        try:
            with p.open("rb") as h:
                h.seek(offset)
                chunk = h.read()
                offset = h.tell()
        except OSError:
            continue
        if not chunk:
            continue
        text = leftover + chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        # Preserve a trailing partial line for the next poll.
        leftover = lines.pop() if lines and not text.endswith("\n") else ""
        for piece in lines:
            # Split on \r so tqdm frames surface as separate lines.
            for sub in piece.split("\r"):
                pump.push(level, sub)


def attach_file_tail(
    path: str,
    *,
    source: str,
    emit: Callable[[Event], None],
    op_run_id: str,
    job_id: str | None = None,
    level: str = "info",
    poll_interval: float = 0.25,
) -> SubprocessLogHandle:
    """Tail a growing log file, emitting one `LogLine` per appended line.

    For backends whose subprocess is detached (e.g. the long-lived
    vllm-mlx server managed by `ServerManager`, which writes to a log
    file rather than a pipe so it survives across CLI invocations).
    Returns a `SubprocessLogHandle`; cancel + await the task in
    `finally` exactly like `attach_subprocess`.
    """
    pump = LinePump(
        source=source, emit=emit, op_run_id=op_run_id, job_id=job_id
    )
    task = asyncio.create_task(
        _tail_file(path, pump=pump, level=level, poll_interval=poll_interval)
    )
    return SubprocessLogHandle(tasks=[task])


class _EventLoggingHandler(logging.Handler):
    """`logging.Handler` that forwards records to a `LogLine` pump."""

    def __init__(
        self,
        *,
        pump: LinePump,
    ) -> None:
        super().__init__()
        self._pump = pump

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            msg = self.format(record)
        except Exception:  # pragma: no cover -- format guard
            msg = record.getMessage()
        level = record.levelname.lower()
        self._pump.push(level, msg)


@dataclass
class LoggerAttachToken:
    """Returned by `attach_logger`. Caller MUST `.detach()` in `finally`."""

    logger: logging.Logger
    handler: logging.Handler

    def detach(self) -> None:
        with contextlib.suppress(Exception):
            self.logger.removeHandler(self.handler)


def attach_logger(
    logger_name: str,
    *,
    source: str,
    emit: Callable[[Event], None],
    op_run_id: str,
    job_id: str | None = None,
    level: int = logging.INFO,
) -> LoggerAttachToken:
    """Install a `logging.Handler` that bridges `logger_name` → `LogLine`.

    Returns a token whose `.detach()` MUST be called in the caller's
    `finally` clause. Leaking handlers across runs would compound until
    each LogLine fires N times — the smoke test in
    `tests/test_runtime_log_pump.py` exercises 100 sequential runs and
    asserts handler count stays bounded.
    """
    pump = LinePump(
        source=source, emit=emit, op_run_id=op_run_id, job_id=job_id
    )
    handler = _EventLoggingHandler(pump=pump)
    handler.setLevel(level)
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    # Don't raise the logger's own level — if the caller hasn't configured
    # it, the records still reach the handler because we set level on the
    # handler itself.
    return LoggerAttachToken(logger=logger, handler=handler)
