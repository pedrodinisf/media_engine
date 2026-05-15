"""Daemon end-to-end tests.

Spins up an in-process ``DaemonServer`` against a tmp ``Engine`` and drives
it with a real ``DaemonClient`` over a Unix socket. Covers connect /
fallback, ping, status, run_op (acquire.upload), get/list/lineage/
resolve_id, subscribe_events, and protocol-version mismatch.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from media_engine.daemon import DaemonClient, DaemonServer
from media_engine.daemon.client import DaemonClientError
from media_engine.daemon.protocol import (
    ErrorResponse,
    PingRequest,
    decode_response,
    encode_frame,
)

# Eagerly register ops so the daemon can dispatch them.
from media_engine.ops.acquire import upload as _upload_op  # noqa: F401
from media_engine.runtime.engine import Engine
from media_engine.runtime.events import Progress

assert _upload_op


@pytest.fixture
async def daemon(engine: Engine) -> AsyncIterator[Path]:
    # Unix sockets are limited to ~104 chars on macOS / ~108 on Linux. The
    # pytest tmp_path lives under /private/var/folders/... which busts the
    # limit; place the socket under /tmp with a short name instead.
    socket_dir = Path(tempfile.mkdtemp(prefix="me-", dir="/tmp"))
    socket_path = socket_dir / "d.sock"
    server = DaemonServer(engine, socket_path)
    await server.start()
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        for _ in range(50):
            if socket_path.exists():
                break
            await asyncio.sleep(0.01)
        yield socket_path
    finally:
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task
        await server.stop()
        shutil.rmtree(socket_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────
# connect / fallback
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_returns_client_when_daemon_up(daemon: Path) -> None:
    client = await DaemonClient.connect(daemon, timeout=2.0)
    assert client is not None
    await client.close()


@pytest.mark.asyncio
async def test_connect_returns_none_when_socket_missing(tmp_path: Path) -> None:
    client = await DaemonClient.connect(tmp_path / "nope.sock", timeout=0.1)
    assert client is None


# ─────────────────────────────────────────────────────────────────
# ping / status
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ping_returns_pid(daemon: Path) -> None:
    client = await DaemonClient.connect(daemon, timeout=2.0)
    assert client is not None
    try:
        pong = await client.ping()
        assert pong.daemon_pid > 0
        assert pong.started_at <= datetime.now(UTC)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_status_returns_engine_info(daemon: Path) -> None:
    client = await DaemonClient.connect(daemon, timeout=2.0)
    assert client is not None
    try:
        s = await client.status()
        assert s.namespace == "default"
        assert s.uptime_seconds >= 0
        assert isinstance(s.loaded_models, list)
    finally:
        await client.close()


# ─────────────────────────────────────────────────────────────────
# run_op + read API
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_op_via_daemon(daemon: Path, sample_mp4: Path) -> None:
    client = await DaemonClient.connect(daemon, timeout=2.0)
    assert client is not None
    try:
        artifacts = await client.run_op(
            "acquire.upload", source_path=str(sample_mp4)
        )
        assert len(artifacts) == 1
        v = artifacts[0]
        assert v.kind.value == "video"

        # Read it back via the daemon
        back = await client.get_artifact(v.id)
        assert back is not None and back.id == v.id

        listed = await client.list_artifacts()
        assert v.id in {a.id for a in listed}

        resolved = await client.resolve_id(v.id[:8])
        assert resolved == v.id

        node = await client.lineage(v.id)
        assert node is not None and node.artifact.id == v.id
    finally:
        await client.close()


# ─────────────────────────────────────────────────────────────────
# error envelope + protocol-version mismatch
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_op_raises_daemon_client_error(daemon: Path) -> None:
    client = await DaemonClient.connect(daemon, timeout=2.0)
    assert client is not None
    try:
        with pytest.raises(DaemonClientError, match="No operation"):
            await client.run_op("never.heard")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_protocol_version_mismatch(daemon: Path) -> None:
    """Send a raw frame with a bumped major version → daemon refuses."""
    reader, writer = await asyncio.open_unix_connection(path=str(daemon))
    try:
        req = PingRequest(request_id=uuid4().hex, protocol_version="9.0")
        writer.write(encode_frame(req))
        await writer.drain()
        line = await reader.readline()
        resp = decode_response(line)
        assert isinstance(resp, ErrorResponse)
        assert "ProtocolVersionMismatch" in resp.error_class
    finally:
        writer.close()
        await writer.wait_closed()


# ─────────────────────────────────────────────────────────────────
# event streaming
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_events_receives_engine_emissions(
    daemon: Path, engine: Engine
) -> None:
    client = await DaemonClient.connect(daemon, timeout=2.0)
    assert client is not None
    try:
        stream = await client.subscribe_events()

        async def emit_after_delay() -> None:
            await asyncio.sleep(0.05)
            engine.event_bus.emit(
                Progress(
                    event_id=uuid4().hex,
                    op_run_id="run-x",
                    timestamp=datetime.now(UTC),
                    fraction=0.5,
                    message="halfway",
                )
            )

        emit_task = asyncio.create_task(emit_after_delay())
        try:
            event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        finally:
            await emit_task
        assert event.type == "progress"
        assert event.fraction == 0.5
    finally:
        await client.close()
