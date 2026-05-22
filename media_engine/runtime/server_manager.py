"""Generic process-backed-backend lifecycle.

Spawns long-running services (vllm-mlx, future: TGI, etc.) as background
subprocesses with PID-file tracking, HTTP health checks, and graceful
SIGTERM-then-SIGKILL shutdown. Each ``ServerManager`` instance manages a
named registry of services rooted at ``{permanent_store}/server-state/``.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
import psutil

SHUTDOWN_TIMEOUT_S = 5.0
_ZOMBIE_STATUSES = {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}


@dataclass
class ServerHealth:
    name: str
    pid: int | None
    running: bool
    healthy: bool
    model: str | None = None
    error: str | None = None


class ServerManagerError(RuntimeError):
    pass


class ServerManager:
    """Lifecycle for named subprocess-backed backends."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # ── Path helpers ──

    def _pid_file(self, name: str) -> Path:
        return self.state_dir / f"{name}.pid"

    def _meta_file(self, name: str) -> Path:
        return self.state_dir / f"{name}.json"

    def _log_file(self, name: str) -> Path:
        return self.state_dir / f"{name}.log"

    # ── Inspection ──

    def pid_of(self, name: str) -> int | None:
        f = self._pid_file(name)
        if not f.exists():
            return None
        try:
            return int(f.read_text().strip())
        except (ValueError, OSError):
            return None

    def is_alive(self, name: str) -> bool:
        pid = self.pid_of(name)
        if pid is None:
            return False
        try:
            proc = psutil.Process(pid)
            return proc.status() not in _ZOMBIE_STATUSES
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def log_path(self, name: str) -> Path:
        return self._log_file(name)

    def log_tail(self, name: str, n: int = 50) -> str:
        f = self._log_file(name)
        if not f.exists():
            return ""
        with f.open("r", errors="replace") as h:
            lines = h.readlines()
        return "".join(lines[-n:])

    # ── Lifecycle ──

    def start(
        self,
        name: str,
        command: list[str],
        *,
        meta: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> int:
        """Spawn a detached subprocess; record PID + meta.

        Idempotent: returns the existing PID if a healthy process is already
        running under this name. Raises if the PID file exists but the
        process is dead (caller should clean it up first via stop()).
        """
        if self.is_alive(name):
            existing_pid = self.pid_of(name)
            assert existing_pid is not None
            return existing_pid

        log = self._log_file(name).open("ab")
        try:
            full_env = {**os.environ, **(env or {})}
            proc = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(cwd) if cwd else None,
                env=full_env,
                start_new_session=True,
            )
        finally:
            log.close()

        self._pid_file(name).write_text(str(proc.pid))
        if meta is not None:
            self._meta_file(name).write_text(json.dumps(meta))
        return proc.pid

    def stop(self, name: str, *, timeout: float = SHUTDOWN_TIMEOUT_S) -> bool:
        """SIGTERM, wait up to ``timeout``, SIGKILL if still alive.

        Returns True if a process was actually terminated, False if there was
        nothing to stop. Cleans up PID + meta files in either case. Treats
        zombie/dead status as terminated so we don't wait the full timeout
        when the original parent hasn't reaped the child yet.
        """
        pid = self.pid_of(name)
        terminated = False
        if pid is not None:
            try:
                proc = psutil.Process(pid)
            except psutil.NoSuchProcess:
                pid = None
            else:
                try:
                    proc.terminate()
                except psutil.NoSuchProcess:
                    pid = None
                else:
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        try:
                            if proc.status() in _ZOMBIE_STATUSES:
                                terminated = True
                                break
                        except psutil.NoSuchProcess:
                            terminated = True
                            break
                        time.sleep(0.02)
                    else:
                        with contextlib.suppress(psutil.NoSuchProcess):
                            proc.kill()
                            terminated = True

        for f in (self._pid_file(name), self._meta_file(name)):
            if f.exists():
                f.unlink(missing_ok=True)
        return terminated

    def restart(
        self,
        name: str,
        command: list[str],
        *,
        meta: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> int:
        self.stop(name)
        return self.start(name, command, meta=meta, env=env, cwd=cwd)

    # ── Health ──

    def health_check(
        self,
        name: str,
        *,
        url: str | None = None,
        timeout: float = 1.0,
    ) -> ServerHealth:
        """Best-effort health check. If ``url`` is provided, also checks HTTP."""
        pid = self.pid_of(name)
        running = self.is_alive(name)
        healthy = running
        error: str | None = None
        meta_model: str | None = None

        if self._meta_file(name).exists():
            try:
                meta = json.loads(self._meta_file(name).read_text())
                meta_model = meta.get("model")
            except (json.JSONDecodeError, OSError):
                pass

        if running and url is not None:
            try:
                resp = httpx.get(url, timeout=timeout)
                if resp.status_code != 200:
                    healthy = False
                    error = f"http {resp.status_code}"
            except httpx.HTTPError as e:
                healthy = False
                error = f"http {type(e).__name__}: {e}"

        return ServerHealth(
            name=name, pid=pid, running=running, healthy=healthy,
            model=meta_model, error=error,
        )

    def wait_until_ready(
        self,
        name: str,
        *,
        url: str,
        timeout: float = 120.0,
        poll_interval: float = 0.5,
        on_progress: Callable[[float, str], None] | None = None,
    ) -> Literal[True]:
        """Block until the named server's HTTP health endpoint returns 200.

        Raises ``ServerManagerError`` on timeout or process death.
        """
        deadline = time.monotonic() + timeout
        last_status = ""
        while time.monotonic() < deadline:
            if not self.is_alive(name):
                raise ServerManagerError(
                    f"server {name!r} died before becoming ready. "
                    f"log tail:\n{self.log_tail(name)}"
                )
            health = self.health_check(name, url=url)
            if health.healthy:
                return True
            if on_progress is not None:
                elapsed = timeout - (deadline - time.monotonic())
                status = f"waiting for {name} ({health.error or 'starting'})"
                if status != last_status:
                    on_progress(elapsed, status)
                    last_status = status
            time.sleep(poll_interval)
        raise ServerManagerError(
            f"server {name!r} did not become healthy within {timeout}s. "
            f"log tail:\n{self.log_tail(name)}"
        )
