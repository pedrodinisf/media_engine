"""``med daemon`` subcommand group.

    med daemon start [--foreground]
    med daemon stop
    med daemon status
    med daemon logs [--tail N]
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from media_engine.config import EngineConfig
from media_engine.daemon import DaemonClient, DaemonServer
from media_engine.runtime.engine import Engine

DAEMON_SHUTDOWN_TIMEOUT = 5.0

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(name="daemon", help="Background daemon for warm Engine sessions.")


def _socket_path(cfg: EngineConfig) -> Path:
    return cfg.daemon_socket or (cfg.config_dir / "daemon.sock")


def _pid_path(cfg: EngineConfig) -> Path:
    return cfg.config_dir / "daemon.pid"


def _log_path(cfg: EngineConfig) -> Path:
    return cfg.config_dir / "daemon.log"


def _read_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@app.command("start")
def cmd_start(
    foreground: Annotated[
        bool, typer.Option("--foreground", "-F", help="Run in the current terminal")
    ] = False,
) -> None:
    """Start the daemon (background by default)."""
    cfg = EngineConfig.load()
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    cfg.validate_storage()
    pid_path = _pid_path(cfg)
    socket_path = _socket_path(cfg)

    existing = _read_pid(pid_path)
    if existing is not None and _is_alive(existing):
        console.print(f"[yellow]daemon already running (pid {existing})[/yellow]")
        raise typer.Exit(0)

    if foreground:
        _run_foreground(cfg, pid_path, socket_path)
        return

    log = _log_path(cfg).open("ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "media_engine.daemon.entry"],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ},
        )
    finally:
        log.close()

    pid_path.write_text(str(proc.pid))

    # Wait for the socket to appear (up to 5 s) so `med daemon start` exits
    # with a usable daemon.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if socket_path.exists():
            client_ok = asyncio.run(_quick_ping(socket_path))
            if client_ok:
                console.print(
                    f"[green]daemon started (pid {proc.pid}, socket {socket_path})[/green]"
                )
                return
        time.sleep(0.1)

    err_console.print(
        f"[red]daemon failed to come up within 5s. log tail:[/red]\n"
        f"{_log_tail(_log_path(cfg))}"
    )
    raise typer.Exit(1)


async def _quick_ping(socket_path: Path) -> bool:
    client = await DaemonClient.connect(socket_path, timeout=0.5)
    if client is None:
        return False
    await client.close()
    return True


def _run_foreground(cfg: EngineConfig, pid_path: Path, socket_path: Path) -> None:
    pid_path.write_text(str(os.getpid()))

    async def _serve() -> None:
        engine = Engine.open_session(cfg)
        server = DaemonServer(engine, socket_path)
        await server.start()
        console.print(
            f"[green]daemon listening on {socket_path} (pid {os.getpid()})[/green]"
        )
        try:
            await server.serve_forever()
        finally:
            engine.close()

    try:
        asyncio.run(_serve())
    finally:
        if pid_path.exists():
            pid_path.unlink(missing_ok=True)


@app.command("stop")
def cmd_stop() -> None:
    """Stop the running daemon (SIGTERM, then SIGKILL after 5 s)."""
    cfg = EngineConfig.load()
    pid_path = _pid_path(cfg)
    socket_path = _socket_path(cfg)

    pid = _read_pid(pid_path)
    if pid is None or not _is_alive(pid):
        console.print("[i]no running daemon[/i]")
        if socket_path.exists():
            socket_path.unlink(missing_ok=True)
        if pid_path.exists():
            pid_path.unlink(missing_ok=True)
        return

    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + DAEMON_SHUTDOWN_TIMEOUT
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            break
        time.sleep(0.1)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    pid_path.unlink(missing_ok=True)
    if socket_path.exists():
        socket_path.unlink(missing_ok=True)
    console.print(f"[green]daemon stopped (pid {pid})[/green]")


@app.command("status")
def cmd_status() -> None:
    """Show daemon status."""
    cfg = EngineConfig.load()
    socket_path = _socket_path(cfg)

    info = asyncio.run(_status_query(socket_path))
    if info is None:
        console.print("[red]daemon not running[/red]")
        raise typer.Exit(1)
    if info.get("__json__"):
        typer.echo(_json.dumps(info, indent=2, default=str))
        return
    table = Table(title="Daemon status")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    for k, v in info.items():
        table.add_row(k, str(v))
    console.print(table)


async def _status_query(socket_path: Path) -> dict[str, object] | None:
    client = await DaemonClient.connect(socket_path, timeout=1.0)
    if client is None:
        return None
    try:
        s = await client.status()
        return {
            "pid": s.daemon_pid,
            "started_at": s.started_at.isoformat(),
            "uptime_seconds": round(s.uptime_seconds, 1),
            "namespace": s.namespace,
            "permanent_store": s.permanent_store,
            "loaded_models": s.loaded_models,
            "subscribers": s.subscriber_count,
        }
    finally:
        await client.close()


def _log_tail(path: Path, n: int = 50) -> str:
    if not path.exists():
        return "(no log file yet)"
    with path.open("r", errors="replace") as h:
        lines = h.readlines()
    return "".join(lines[-n:])


@app.command("logs")
def cmd_logs(
    tail: Annotated[int, typer.Option("--tail", "-n")] = 50,
) -> None:
    """Print the tail of the daemon log."""
    cfg = EngineConfig.load()
    typer.echo(_log_tail(_log_path(cfg), n=tail))
