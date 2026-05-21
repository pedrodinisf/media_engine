"""Health + readiness checks.

Two probes (the conventional k8s split):

- ``health()`` — *am I alive?* Returns 200-equivalent unconditionally.
  Kubelet uses this to decide whether to restart the pod.
- ``readiness()`` — *am I ready to serve?* Returns the status of each
  external dependency (cache reachable, permanent_store writable,
  daemon socket present where expected). Kubelet uses this to gate
  traffic.

The structured ``HealthReport`` is shared by ``api/health.py`` (HTTP)
and ``cli/health.py`` (terminal); both formats agree on the schema.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from media_engine.config import EngineConfig

Status = Literal["ok", "degraded", "down"]


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""


@dataclass
class HealthReport:
    alive: bool
    ready: bool
    checks: list[CheckResult] = field(default_factory=lambda: [])  # noqa: PIE807
    version: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "alive": self.alive,
            "ready": self.ready,
            "version": self.version,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail}
                for c in self.checks
            ],
        }


def liveness() -> HealthReport:
    """Always alive — the fact that this call returned proves it."""
    from media_engine import __version__

    return HealthReport(alive=True, ready=True, version=__version__)


def readiness(config: EngineConfig | None = None) -> HealthReport:
    """Inspect external dependencies; return a structured report.

    Worst-case: ``ready=False`` when any check is ``"down"``;
    ``ready=True`` when every check is ``"ok"``; otherwise ``True`` but
    with ``"degraded"`` entries so operators can see what's wobbly
    without taking pods out of rotation.
    """
    from media_engine import __version__

    cfg = config or EngineConfig.load()
    checks: list[CheckResult] = []

    # 1. permanent_store writable
    checks.append(_check_storage_writable(cfg.permanent_store))

    # 2. cache reachable (single SELECT 1 — cheap on both dialects)
    checks.append(_check_cache_reachable(cfg.resolve_cache_db_url()))

    # 3. daemon socket (only relevant when configured + present)
    if cfg.daemon_socket is not None or (cfg.config_dir / "daemon.sock").exists():
        checks.append(
            _check_daemon_socket(
                cfg.daemon_socket or (cfg.config_dir / "daemon.sock")
            )
        )

    any_down = any(c.status == "down" for c in checks)
    return HealthReport(
        alive=True,
        ready=not any_down,
        checks=checks,
        version=__version__,
    )


def _check_storage_writable(path: Path) -> CheckResult:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return CheckResult(
            name="permanent_store",
            status="down",
            detail=f"cannot create {path}: {e}",
        )
    if not os.access(path, os.W_OK):
        return CheckResult(
            name="permanent_store",
            status="down",
            detail=f"{path} is not writable",
        )
    return CheckResult(name="permanent_store", status="ok", detail=str(path))


def _check_cache_reachable(db_url: str) -> CheckResult:
    from sqlalchemy import create_engine, text

    try:
        engine = create_engine(db_url, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001 -- defensive; any error → down
        return CheckResult(
            name="cache_db", status="down", detail=f"{type(e).__name__}: {e}"
        )
    return CheckResult(name="cache_db", status="ok", detail=db_url)


def _check_daemon_socket(socket_path: Path) -> CheckResult:
    if not socket_path.exists():
        return CheckResult(
            name="daemon_socket",
            status="degraded",
            detail=f"socket not present at {socket_path}",
        )
    return CheckResult(
        name="daemon_socket", status="ok", detail=str(socket_path)
    )
