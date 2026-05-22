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
import shutil
import uuid
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

    # 1. permanent_store writable (real write+delete, not just os.W_OK)
    checks.append(_check_storage_writable(cfg.permanent_store))

    # 2. free space ≥ min_free_gb — without this, the probe stays green
    #    while writes start failing with the engine's disk-guard error.
    checks.append(
        _check_free_space(cfg.permanent_store, min_free_gb=cfg.min_free_gb)
    )

    # 3. cache reachable (single SELECT 1 — cheap on both dialects)
    checks.append(_check_cache_reachable(cfg.resolve_cache_db_url()))

    # 4. daemon socket (only relevant when configured + present)
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
    """Probe by actually writing+deleting a small file.

    ``os.access(..., os.W_OK)`` consults permission bits only and
    silently passes on read-only mounts, exhausted inodes, ACL
    overrides, and similar real-world denials. A round-trip with the
    actual filesystem is the only honest "writable" check.
    """
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
    probe = path / f".health-probe-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_bytes(b"ok")
    except OSError as e:
        return CheckResult(
            name="permanent_store",
            status="down",
            detail=f"write probe failed at {path}: {e}",
        )
    finally:
        probe.unlink(missing_ok=True)
    return CheckResult(name="permanent_store", status="ok", detail=str(path))


def _check_free_space(path: Path, *, min_free_gb: float) -> CheckResult:
    """Gate readiness on free disk space matching the engine's disk-guard.

    The engine refuses to start jobs when free space drops below
    ``min_free_gb``; surfacing that in readiness keeps clients out of a
    pod that would only produce ``DiskFullError`` until cleanup runs.
    Reports ``degraded`` between the guard threshold and 2x it so
    operators see warning without traffic being yanked.
    """
    if min_free_gb <= 0:
        return CheckResult(
            name="free_space",
            status="ok",
            detail="min_free_gb disabled",
        )
    try:
        usage = shutil.disk_usage(path)
    except OSError as e:
        return CheckResult(
            name="free_space",
            status="degraded",
            detail=f"cannot stat {path}: {e}",
        )
    free_gb = usage.free / (1024 ** 3)
    if free_gb < min_free_gb:
        return CheckResult(
            name="free_space",
            status="down",
            detail=(
                f"{free_gb:.2f} GB free at {path}; threshold "
                f"{min_free_gb} GB"
            ),
        )
    if free_gb < min_free_gb * 2:
        return CheckResult(
            name="free_space",
            status="degraded",
            detail=(
                f"{free_gb:.2f} GB free at {path}; approaching "
                f"{min_free_gb} GB threshold"
            ),
        )
    return CheckResult(
        name="free_space",
        status="ok",
        detail=f"{free_gb:.2f} GB free at {path}",
    )


def _check_cache_reachable(db_url: str) -> CheckResult:
    """Probe the cache with a trivial ``SELECT 1``.

    The probe creates a one-shot SQLAlchemy engine and disposes it
    immediately — readiness can be polled every few seconds by
    kubelet, and a per-probe engine without ``dispose`` would slowly
    leak pool connections.
    """
    from sqlalchemy import create_engine, text

    engine = None
    try:
        engine = create_engine(db_url, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001 -- defensive; any error → down
        return CheckResult(
            name="cache_db", status="down", detail=f"{type(e).__name__}: {e}"
        )
    finally:
        if engine is not None:
            engine.dispose()
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
