"""Tests for the Phase 1 audit follow-up fixes.

Covers: bootstrap full-catalog registration, CLI↔daemon auto-routing,
bidirectional protocol-version check, daemon subprocess lifecycle, and
compile-time cycle detection.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from media_engine.bootstrap import register_all
from media_engine.cli._handle import (
    DaemonEngineHandle,
    LocalEngineHandle,
    open_handle,
)
from media_engine.config import EngineConfig
from media_engine.daemon import DaemonServer
from media_engine.daemon.client import DaemonClient, ProtocolVersionMismatch
from media_engine.ops import OpRegistry
from media_engine.profiles.pipeline import ProfileCompileError, compile_pipeline_profile
from media_engine.profiles.schema import GraphNodeSpec, InputSpec, PipelineProfile
from media_engine.runtime.engine import Engine

# ─────────────────────────────────────────────────────────────────
# bootstrap.register_all — full catalog
# ─────────────────────────────────────────────────────────────────


def test_register_all_populates_full_op_catalog() -> None:
    register_all(force=True)
    names = {op.name for op in OpRegistry.list_all()}
    # Every Phase 1 op must be visible (not just acquire.upload +
    # video.extract_audio that the CLI used to import).
    expected = {
        "acquire.upload",
        "audio.detect_language",
        "audio.diarize",
        "audio.transcribe",
        "audio.transcribe_diarized",
        "chunk.semantic",
        "embed.text",
        "frames.subsample",
        "video.extract_audio",
        "video.sample_frames",
        "video.trim",
    }
    assert expected.issubset(names)


def test_register_all_registers_pyscenedetect_backend() -> None:
    from media_engine.backends import BackendRegistry

    register_all(force=True)
    # pyscenedetect imports cleanly even without the scenedetect lib, so it
    # must be registered (the dep is only needed at execute() time).
    assert BackendRegistry.has("video.sample_frames", "pyscenedetect")
    assert BackendRegistry.has("video.sample_frames", "ffmpeg-uniform")


def test_register_all_idempotent() -> None:
    register_all(force=True)
    before = len(OpRegistry.list_all())
    register_all(force=True)
    assert len(OpRegistry.list_all()) == before


# ─────────────────────────────────────────────────────────────────
# CLI ↔ daemon auto-routing (open_handle)
# ─────────────────────────────────────────────────────────────────


@pytest.fixture
def daemon_socket(engine: Engine) -> AsyncIterator[Path]:
    """An in-process DaemonServer on a short /tmp socket."""
    socket_dir = Path(tempfile.mkdtemp(prefix="me-aud-", dir="/tmp"))
    socket_path = socket_dir / "d.sock"

    async def _run() -> tuple[DaemonServer, asyncio.Task[None]]:
        server = DaemonServer(engine, socket_path)
        await server.start()
        task = asyncio.create_task(server.serve_forever())
        for _ in range(50):
            if socket_path.exists():
                break
            await asyncio.sleep(0.01)
        return server, task

    server, task = asyncio.run(_run())
    try:
        yield socket_path
    finally:
        async def _stop() -> None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await server.stop()

        asyncio.run(_stop())
        shutil.rmtree(socket_dir, ignore_errors=True)


async def test_open_handle_falls_back_to_local_when_no_daemon(
    engine_config: EngineConfig,
) -> None:
    async with open_handle(engine_config) as h:
        assert isinstance(h, LocalEngineHandle)
        assert h.routed_via_daemon is False


async def test_open_handle_routes_through_running_daemon(
    engine: Engine, engine_config: EngineConfig
) -> None:
    """When a daemon is up at the configured socket, the handle is daemon-routed."""
    socket_dir = Path(tempfile.mkdtemp(prefix="me-aud2-", dir="/tmp"))
    socket_path = socket_dir / "d.sock"
    cfg = engine_config.model_copy(update={"daemon_socket": socket_path})

    server = DaemonServer(engine, socket_path)
    await server.start()
    serve = asyncio.create_task(server.serve_forever())
    try:
        for _ in range(50):
            if socket_path.exists():
                break
            await asyncio.sleep(0.01)
        async with open_handle(cfg, ping_timeout=2.0) as h:
            assert isinstance(h, DaemonEngineHandle)
            assert h.routed_via_daemon is True
            # A read actually goes over the socket.
            rows = await h.list_artifacts()
            assert rows == []
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve
        await server.stop()
        shutil.rmtree(socket_dir, ignore_errors=True)


async def test_daemon_routed_run_op_persists_to_shared_cache(
    engine: Engine, engine_config: EngineConfig, sample_mp4: Path
) -> None:
    """An op run through the daemon lands in the same cache.db a local
    engine reads — the property the whole routing design rests on."""
    socket_dir = Path(tempfile.mkdtemp(prefix="me-aud3-", dir="/tmp"))
    socket_path = socket_dir / "d.sock"
    cfg = engine_config.model_copy(update={"daemon_socket": socket_path})

    server = DaemonServer(engine, socket_path)
    await server.start()
    serve = asyncio.create_task(server.serve_forever())
    try:
        for _ in range(50):
            if socket_path.exists():
                break
            await asyncio.sleep(0.01)
        async with open_handle(cfg, ping_timeout=2.0) as h:
            assert isinstance(h, DaemonEngineHandle)
            [vid] = await h.run("acquire.upload", source_path=str(sample_mp4))
        # A fresh *local* engine on the same cfg sees the daemon's output.
        with Engine.open_quick(cfg) as local:
            assert local.get_artifact(vid.id) is not None
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve
        await server.stop()
        shutil.rmtree(socket_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────
# Bidirectional protocol-version check
# ─────────────────────────────────────────────────────────────────


async def test_client_connect_raises_on_daemon_version_mismatch(
    engine: Engine,
) -> None:
    """Patch the daemon's PingResponse to a future major; connect() must
    raise ProtocolVersionMismatch (NOT silently return None)."""
    import media_engine.daemon.server as server_mod

    socket_dir = Path(tempfile.mkdtemp(prefix="me-pv-", dir="/tmp"))
    socket_path = socket_dir / "d.sock"
    server = DaemonServer(engine, socket_path)
    await server.start()
    serve = asyncio.create_task(server.serve_forever())

    real_ping_response = server_mod.PingResponse

    def _future_ping(**kw: object) -> object:
        kw["protocol_version"] = "9.0"
        return real_ping_response(**kw)  # type: ignore[arg-type]

    try:
        for _ in range(50):
            if socket_path.exists():
                break
            await asyncio.sleep(0.01)
        server_mod.PingResponse = _future_ping  # type: ignore[assignment,misc]
        with pytest.raises(ProtocolVersionMismatch, match="med daemon stop"):
            await DaemonClient.connect(socket_path, timeout=2.0)
    finally:
        server_mod.PingResponse = real_ping_response  # type: ignore[misc]
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve
        await server.stop()
        shutil.rmtree(socket_dir, ignore_errors=True)


async def test_client_connect_returns_none_when_socket_absent(
    tmp_path: Path,
) -> None:
    assert await DaemonClient.connect(tmp_path / "nope.sock", timeout=0.1) is None


# ─────────────────────────────────────────────────────────────────
# Daemon subprocess lifecycle (real `python -m media_engine.daemon.entry`)
# ─────────────────────────────────────────────────────────────────


def test_daemon_subprocess_start_ping_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spawn the real entry module as a detached process, ping it over the
    socket, then SIGTERM it and confirm no orphan."""
    store = tmp_path / "store"
    work = tmp_path / "work"
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True)
    # Socket under /tmp to dodge the 104-char AF_UNIX limit.
    socket_dir = Path(tempfile.mkdtemp(prefix="me-sub-", dir="/tmp"))
    socket_path = socket_dir / "d.sock"

    env = {
        **os.environ,
        "MEDIA_ENGINE_PERMANENT_STORE": str(store),
        "MEDIA_ENGINE_WORKDIR": str(work),
        "MEDIA_ENGINE_CACHE_DB_URL": f"sqlite+pysqlite:///{tmp_path / 'c.db'}",
        "MEDIA_ENGINE_CONFIG_DIR": str(cfg_dir),
        "MEDIA_ENGINE_DAEMON_SOCKET": str(socket_path),
        "MEDIA_ENGINE_MIN_FREE_GB": "0",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "media_engine.daemon.entry"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        # Wait for the socket to appear (entry prints "daemon listening …").
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not socket_path.exists():
            if proc.poll() is not None:
                out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                pytest.fail(f"daemon entry died before listening:\n{out}")
            time.sleep(0.1)
        assert socket_path.exists(), "daemon never created its socket"

        async def _ping() -> bool:
            client = await DaemonClient.connect(socket_path, timeout=3.0)
            if client is None:
                return False
            pong = await client.ping()
            await client.close()
            return pong.daemon_pid == proc.pid

        assert asyncio.run(_ping())
    finally:
        # Graceful SIGTERM (entry installs a handler) then hard-kill.
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
        shutil.rmtree(socket_dir, ignore_errors=True)

    # No orphan: the PID is gone.
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)


# ─────────────────────────────────────────────────────────────────
# Compile-time cycle detection
# ─────────────────────────────────────────────────────────────────


def _video(tmp_path: Path):
    from datetime import UTC, datetime

    from media_engine.artifacts import Video

    f = tmp_path / "v.mp4"
    f.write_bytes(b"\x00")
    return Video(id="v" * 64, path=f, created_at=datetime.now(UTC))


def test_profile_cycle_fails_at_compile_not_runtime(tmp_path: Path) -> None:
    profile = PipelineProfile(
        name="cyclic",
        inputs=[InputSpec(name="source", kind="video")],
        graph=[
            GraphNodeSpec(id="a", op="video.extract_audio",
                          inputs=["b"]),
            GraphNodeSpec(id="b", op="video.extract_audio",
                          inputs=["a"]),
        ],
    )
    with pytest.raises(ProfileCompileError, match="invalid graph"):
        compile_pipeline_profile(profile, sources={"source": _video(tmp_path)})


def test_profile_unresolved_ref_fails_at_compile(tmp_path: Path) -> None:
    profile = PipelineProfile(
        name="dangling",
        inputs=[InputSpec(name="source", kind="video")],
        graph=[
            GraphNodeSpec(id="a", op="video.extract_audio",
                          inputs=["does_not_exist"]),
        ],
    )
    with pytest.raises(ProfileCompileError, match="invalid graph"):
        compile_pipeline_profile(profile, sources={"source": _video(tmp_path)})


def test_valid_profile_still_compiles(tmp_path: Path) -> None:
    profile = PipelineProfile(
        name="ok",
        inputs=[InputSpec(name="source", kind="video")],
        graph=[
            GraphNodeSpec(id="audio", op="video.extract_audio",
                          inputs=["source"]),
        ],
    )
    pipe = compile_pipeline_profile(profile, sources={"source": _video(tmp_path)})
    assert [n.id for n in pipe.nodes] == ["audio"]
