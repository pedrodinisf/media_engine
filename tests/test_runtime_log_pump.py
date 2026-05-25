"""Unit tests for `media_engine.runtime.log_pump`."""

from __future__ import annotations

import asyncio
import logging

import pytest

from media_engine.runtime.events import Event, LogLine
from media_engine.runtime.log_pump import (
    MAX_LINES_PER_RUN,
    attach_logger,
    attach_subprocess,
)


@pytest.mark.asyncio
async def test_attach_subprocess_emits_one_logline_per_stdout_line() -> None:
    """A subprocess printing 3 lines → 3 LogLine events on the bus."""
    captured: list[Event] = []
    # Use `python -c` to keep the test cross-platform — no shell required.
    proc = await asyncio.create_subprocess_exec(
        "python",
        "-c",
        "import sys; sys.stdout.write('alpha\\nbeta\\ngamma\\n'); sys.stdout.flush()",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    handle = attach_subprocess(
        proc, source="test-stdout", emit=captured.append, op_run_id="op-1"
    )
    await proc.wait()
    await handle.aclose()

    lines = [
        ev.line for ev in captured if isinstance(ev, LogLine) and ev.source == "test-stdout"
    ]
    assert lines == ["alpha", "beta", "gamma"], lines
    for ev in captured:
        if isinstance(ev, LogLine):
            assert ev.op_run_id == "op-1"
            assert ev.level == "info"


@pytest.mark.asyncio
async def test_attach_subprocess_collapses_consecutive_duplicates() -> None:
    """tqdm-style identical lines collapse to a single emission."""
    captured: list[Event] = []
    proc = await asyncio.create_subprocess_exec(
        "python",
        "-c",
        "import sys; sys.stdout.write('same\\nsame\\nsame\\nother\\n'); sys.stdout.flush()",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    handle = attach_subprocess(
        proc, source="dedup", emit=captured.append, op_run_id="op"
    )
    await proc.wait()
    await handle.aclose()

    lines = [ev.line for ev in captured if isinstance(ev, LogLine)]
    assert lines == ["same", "other"], lines


@pytest.mark.asyncio
async def test_attach_subprocess_caps_at_max_lines() -> None:
    """Past the cap, one final 'log truncated' warn is emitted then quiet."""
    captured: list[Event] = []
    # Print MAX_LINES_PER_RUN + 50 distinct lines (use the index to defeat
    # the dedup so every line counts toward the cap).
    n = MAX_LINES_PER_RUN + 50
    proc = await asyncio.create_subprocess_exec(
        "python",
        "-c",
        f"import sys\n"
        f"for i in range({n}):\n"
        f"    sys.stdout.write(f'line-' + str(i) + '\\n')\n",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    handle = attach_subprocess(
        proc, source="cap", emit=captured.append, op_run_id="op"
    )
    await proc.wait()
    await handle.aclose()

    log_events = [ev for ev in captured if isinstance(ev, LogLine)]
    # MAX_LINES_PER_RUN info lines + one warn truncation marker.
    info_count = sum(1 for ev in log_events if ev.level == "info")
    warn_count = sum(1 for ev in log_events if ev.level == "warn")
    assert info_count == MAX_LINES_PER_RUN, info_count
    assert warn_count == 1, warn_count
    assert "truncated" in log_events[-1].line


@pytest.mark.asyncio
async def test_attach_logger_bridges_records_to_logline() -> None:
    """Records sent to the named logger surface as LogLine events."""
    captured: list[Event] = []
    logger_name = "test_attach_logger_bridge"
    token = attach_logger(
        logger_name,
        source="bridge",
        emit=captured.append,
        op_run_id="op",
        level=logging.DEBUG,
    )
    try:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.DEBUG)
        logger.info("hello world")
        logger.warning("be careful")
    finally:
        token.detach()

    log_events = [ev for ev in captured if isinstance(ev, LogLine)]
    assert len(log_events) == 2
    assert log_events[0].line == "hello world"
    assert log_events[0].level == "info"
    assert log_events[1].line == "be careful"
    assert log_events[1].level == "warning"


@pytest.mark.asyncio
async def test_attach_logger_detach_removes_handler() -> None:
    """detach() must remove the handler — running 100 attach/detach cycles
    leaves the logger's handler list bounded (no leak)."""
    captured: list[Event] = []
    logger_name = "test_attach_logger_no_leak"
    logger = logging.getLogger(logger_name)
    baseline = len(logger.handlers)

    for _ in range(100):
        token = attach_logger(
            logger_name,
            source="leak-test",
            emit=captured.append,
            op_run_id="op",
        )
        token.detach()

    assert len(logger.handlers) == baseline, (
        f"handler leak: baseline={baseline} now={len(logger.handlers)}"
    )
